// Standalone validation of the parsing logic from FFGEXLevels.cs.
// Re-implements ParseSnapshot using System.Text.Json (built-in) instead of
// Newtonsoft.Json (NT8-bundled). The parsing LOGIC is what we're validating;
// the JSON library is interchangeable.

using System;
using System.Collections.Generic;
using System.IO;
using System.Text.Json;

public enum FFGEXMappingMode { DynamicMultiplier, CarryBasis, RawETFStrike }

public class Snapshot
{
    public DateTime GeneratedAtUtc;
    public string Ticker = "";
    public string Status = "";
    public double Spot;
    public double Multiplier;
    public double? Flip;
    public Level CallWall;
    public Level PutWall;
    public List<Level> PosClusters = new();
    public List<Level> NegClusters = new();
    public List<Level> OIClusters = new();
    public List<string> Warnings = new();
    public int ContractCount;

    public class Level
    {
        public double Price;
        public double EtfStrike;
        public double Magnitude;
        public bool IsOI;
    }
}

public static class Parser
{
    public static Snapshot Parse(string json, FFGEXMappingMode mode)
    {
        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;
        var s = new Snapshot();
        if (root.TryGetProperty("ticker", out var t)) s.Ticker = t.GetString() ?? "";
        if (root.TryGetProperty("status", out var st)) s.Status = st.GetString() ?? "";
        if (root.TryGetProperty("generated_at", out var ga))
            s.GeneratedAtUtc = ParseUtc(ga.GetString());
        if (root.TryGetProperty("spot", out var sp) && sp.ValueKind != JsonValueKind.Null)
            s.Spot = sp.GetDouble();
        if (root.TryGetProperty("multiplier", out var mu) && mu.ValueKind != JsonValueKind.Null)
            s.Multiplier = mu.GetDouble();
        if (root.TryGetProperty("contract_count", out var cc) && cc.ValueKind != JsonValueKind.Null)
            s.ContractCount = cc.GetInt32();
        if (root.TryGetProperty("warnings", out var ws) && ws.ValueKind == JsonValueKind.Array)
        {
            foreach (var w in ws.EnumerateArray())
                s.Warnings.Add(w.GetString() ?? "");
        }

        if (s.Status != "ok") return s;

        if (root.TryGetProperty("gamma_flip", out var gf) && gf.ValueKind != JsonValueKind.Null)
            s.Flip = MapPrice(gf, mode);

        if (root.TryGetProperty("call_wall", out var cw) && cw.ValueKind != JsonValueKind.Null)
            s.CallWall = NodeToLevel(cw, mode, isOI: false);

        if (root.TryGetProperty("put_wall", out var pw) && pw.ValueKind != JsonValueKind.Null)
            s.PutWall = NodeToLevel(pw, mode, isOI: false);

        AppendClusters(root, "top_pos_clusters", s.PosClusters, mode, false);
        AppendClusters(root, "top_neg_clusters", s.NegClusters, mode, false);
        AppendClusters(root, "top_oi_clusters", s.OIClusters, mode, true);

        return s;
    }

    static void AppendClusters(JsonElement root, string key, List<Snapshot.Level> sink,
        FFGEXMappingMode mode, bool isOI)
    {
        if (!root.TryGetProperty(key, out var arr) || arr.ValueKind != JsonValueKind.Array)
            return;
        foreach (var item in arr.EnumerateArray())
        {
            var lvl = NodeToLevel(item, mode, isOI);
            if (lvl != null) sink.Add(lvl);
        }
    }

    static Snapshot.Level NodeToLevel(JsonElement node, FFGEXMappingMode mode, bool isOI)
    {
        double? p = MapPrice(node, mode);
        if (!p.HasValue) return null;
        var lvl = new Snapshot.Level
        {
            Price = p.Value,
            EtfStrike = node.TryGetProperty("etf_strike", out var es) ? es.GetDouble() : 0,
            IsOI = isOI,
            Magnitude = isOI
                ? Math.Abs(node.TryGetProperty("open_interest", out var oi) ? oi.GetDouble() : 0)
                : Math.Abs(node.TryGetProperty("gex_dollars", out var gd) ? gd.GetDouble() : 0),
        };
        return lvl;
    }

    static double? MapPrice(JsonElement node, FFGEXMappingMode mode)
    {
        string key = mode switch
        {
            FFGEXMappingMode.DynamicMultiplier => "futures_mult",
            FFGEXMappingMode.CarryBasis => "futures_basis",
            FFGEXMappingMode.RawETFStrike => "etf_strike",
            _ => "futures_mult",
        };
        if (!node.TryGetProperty(key, out var tok) || tok.ValueKind == JsonValueKind.Null)
            return null;
        return tok.GetDouble();
    }

    static DateTime ParseUtc(string iso)
    {
        if (string.IsNullOrEmpty(iso)) return DateTime.MinValue;
        return DateTime.TryParse(iso, System.Globalization.CultureInfo.InvariantCulture,
            System.Globalization.DateTimeStyles.AdjustToUniversal | System.Globalization.DateTimeStyles.AssumeUniversal,
            out var d) ? d.ToUniversalTime() : DateTime.MinValue;
    }
}

class Program
{
    static int passed = 0, failed = 0;

    static void Assert(bool cond, string msg)
    {
        if (cond) { passed++; Console.WriteLine($"  PASS: {msg}"); }
        else { failed++; Console.WriteLine($"  FAIL: {msg}"); }
    }

    static void AssertEq<T>(T expected, T actual, string msg) where T : IEquatable<T>
    {
        bool ok = expected?.Equals(actual) ?? actual == null;
        if (ok) { passed++; Console.WriteLine($"  PASS: {msg} = {actual}"); }
        else { failed++; Console.WriteLine($"  FAIL: {msg}: expected {expected}, got {actual}"); }
    }

    static void AssertClose(double expected, double actual, double tol, string msg)
    {
        bool ok = Math.Abs(expected - actual) <= tol;
        if (ok) { passed++; Console.WriteLine($"  PASS: {msg} = {actual:F4} (expected {expected:F4})"); }
        else { failed++; Console.WriteLine($"  FAIL: {msg}: expected {expected:F4}, got {actual:F4}"); }
    }

    static int Main(string[] args)
    {
        string fixturePath = args.Length > 0 ? args[0] : "spy_payload.json";
        if (!File.Exists(fixturePath))
        {
            Console.Error.WriteLine($"Fixture not found: {fixturePath}");
            return 2;
        }
        var json = File.ReadAllText(fixturePath);

        Console.WriteLine("== DynamicMultiplier ==");
        {
            var s = Parser.Parse(json, FFGEXMappingMode.DynamicMultiplier);
            AssertEq("ok", s.Status, "status");
            AssertEq("SPY", s.Ticker, "ticker");
            AssertClose(583.42, s.Spot, 0.01, "spot");
            AssertClose(9.9414, s.Multiplier, 0.001, "multiplier");
            Assert(s.Flip.HasValue, "flip has value");
            Assert(s.CallWall != null, "call wall not null");
            Assert(s.PutWall != null, "put wall not null");
            AssertClose(5865.41, s.CallWall.Price, 0.1, "CW price (mult)");
            AssertEq(590.0, s.CallWall.EtfStrike, "CW etf_strike");
            Assert(s.CallWall.Magnitude > 1e9, "CW magnitude > 1B");
            AssertClose(5716.29, s.PutWall.Price, 0.1, "PW price (mult)");
            AssertEq(575.0, s.PutWall.EtfStrike, "PW etf_strike");
            Assert(s.PosClusters.Count > 0, "have pos clusters");
            Assert(s.NegClusters.Count > 0, "have neg clusters");
            Assert(s.OIClusters.Count > 0, "have OI clusters");
            Assert(s.OIClusters[0].IsOI, "OI cluster flagged");
            Assert(s.OIClusters[0].Magnitude > 1000, "OI magnitude > 1000");
        }

        Console.WriteLine("\n== CarryBasis ==");
        {
            var s = Parser.Parse(json, FFGEXMappingMode.CarryBasis);
            AssertClose(5914.79, s.CallWall.Price, 0.1, "CW price (basis)");
            AssertClose(5764.79, s.PutWall.Price, 0.1, "PW price (basis)");
        }

        Console.WriteLine("\n== RawETFStrike ==");
        {
            var s = Parser.Parse(json, FFGEXMappingMode.RawETFStrike);
            AssertClose(590.0, s.CallWall.Price, 0.001, "CW price (raw)");
            AssertClose(575.0, s.PutWall.Price, 0.001, "PW price (raw)");
        }

        Console.WriteLine("\n== Error payload ==");
        {
            string errJson = "{\"ticker\":\"SPY\",\"status\":\"error\",\"warnings\":[\"fetch failed\"]}";
            var s = Parser.Parse(errJson, FFGEXMappingMode.DynamicMultiplier);
            AssertEq("error", s.Status, "error status");
            AssertEq(1, s.Warnings.Count, "warning count");
            Assert(s.CallWall == null, "no call wall on error");
            Assert(s.PutWall == null, "no put wall on error");
            Assert(!s.Flip.HasValue, "no flip on error");
        }

        Console.WriteLine("\n== Missing fields ==");
        {
            string sparse = "{\"ticker\":\"SPY\",\"status\":\"ok\",\"spot\":100.0," +
                "\"multiplier\":10.0,\"contract_count\":50}";
            var s = Parser.Parse(sparse, FFGEXMappingMode.DynamicMultiplier);
            AssertEq("ok", s.Status, "ok status sparse");
            AssertEq(50, s.ContractCount, "contract count sparse");
            Assert(!s.Flip.HasValue, "flip missing");
            Assert(s.CallWall == null, "CW missing");
            Assert(s.PosClusters.Count == 0, "no pos clusters");
        }

        Console.WriteLine($"\n{passed} passed, {failed} failed");
        return failed == 0 ? 0 : 1;
    }
}
