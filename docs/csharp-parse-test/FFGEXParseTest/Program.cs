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
    // v2 0DTE intraday levels (any may be null on thin chains)
    public Level Dte0CallRes;
    public Level Dte0PutSup;
    public Level Dte0Hvl;
    public Level Dte0GammaWall;
    // v2 1-day expected-move band
    public double? ExpMoveHigh;
    public double? ExpMoveLow;

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

        // v2 nests the structural levels under "blended"; v1 had them at root.
        // Fall back to root so cached v1 payloads still parse.
        JsonElement levels = root;
        if (root.TryGetProperty("blended", out var bl) && bl.ValueKind == JsonValueKind.Object)
            levels = bl;

        if (levels.TryGetProperty("gamma_flip", out var gf) && gf.ValueKind != JsonValueKind.Null)
            s.Flip = MapPrice(gf, mode);

        if (levels.TryGetProperty("call_wall", out var cw) && cw.ValueKind != JsonValueKind.Null)
            s.CallWall = NodeToLevel(cw, mode, isOI: false);

        if (levels.TryGetProperty("put_wall", out var pw) && pw.ValueKind != JsonValueKind.Null)
            s.PutWall = NodeToLevel(pw, mode, isOI: false);

        AppendClusters(levels, "top_pos_clusters", s.PosClusters, mode, false);
        AppendClusters(levels, "top_neg_clusters", s.NegClusters, mode, false);
        AppendClusters(levels, "top_oi_clusters", s.OIClusters, mode, true);

        // v2 0DTE block (null when no same-day expiry; sides may be null)
        if (root.TryGetProperty("dte0", out var dte0) && dte0.ValueKind == JsonValueKind.Object)
        {
            s.Dte0CallRes   = SubLevel(dte0, "call_resistance_0dte", mode);
            s.Dte0PutSup    = SubLevel(dte0, "put_support_0dte", mode);
            s.Dte0Hvl       = SubLevel(dte0, "hvl_0dte", mode);
            s.Dte0GammaWall = SubLevel(dte0, "gamma_wall_0dte", mode);
        }

        // v2 1-day expected-move band
        if (root.TryGetProperty("expected_move_1d", out var em) && em.ValueKind == JsonValueKind.Object)
        {
            s.ExpMoveHigh = MapMovePrice(em, mode, high: true);
            s.ExpMoveLow  = MapMovePrice(em, mode, high: false);
        }

        return s;
    }

    static Snapshot.Level SubLevel(JsonElement parent, string key, FFGEXMappingMode mode)
    {
        if (!parent.TryGetProperty(key, out var node) || node.ValueKind == JsonValueKind.Null)
            return null;
        return NodeToLevel(node, mode, isOI: false);
    }

    static double? MapMovePrice(JsonElement em, FFGEXMappingMode mode, bool high)
    {
        string key = mode == FFGEXMappingMode.RawETFStrike
            ? (high ? "high_etf" : "low_etf")
            : (high ? "high_futures_mult" : "low_futures_mult");
        if (!em.TryGetProperty(key, out var tok) || tok.ValueKind == JsonValueKind.Null)
            return null;
        return tok.GetDouble();
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

        Console.WriteLine("\n== v2 schema (blended + dte0 + expected_move) ==");
        {
            const string v2Path = "spy_payload_v2.json";
            if (!File.Exists(v2Path))
            {
                Console.Error.WriteLine($"v2 fixture not found: {v2Path}");
                return 2;
            }
            var v2 = File.ReadAllText(v2Path);

            // DynamicMultiplier: structural levels must now come from "blended".
            var s = Parser.Parse(v2, FFGEXMappingMode.DynamicMultiplier);
            AssertEq("ok", s.Status, "v2 status");
            Assert(s.CallWall != null, "v2 blended call wall not null");
            Assert(s.PutWall != null, "v2 blended put wall not null");
            AssertClose(5865.41, s.CallWall.Price, 0.1, "v2 blended CW price (mult)");
            AssertEq(590.0, s.CallWall.EtfStrike, "v2 blended CW etf_strike");
            AssertClose(5716.29, s.PutWall.Price, 0.1, "v2 blended PW price (mult)");
            Assert(s.Flip.HasValue, "v2 blended flip has value");
            Assert(s.PosClusters.Count > 0, "v2 have pos clusters");
            Assert(s.OIClusters.Count > 0, "v2 have OI clusters (blended)");

            // 0DTE intraday levels.
            Assert(s.Dte0CallRes != null, "v2 dte0 call resistance not null");
            Assert(s.Dte0PutSup != null, "v2 dte0 put support not null");
            Assert(s.Dte0Hvl != null, "v2 dte0 hvl not null");
            Assert(s.Dte0GammaWall != null, "v2 dte0 gamma wall not null");
            AssertClose(5865.41, s.Dte0CallRes.Price, 0.1, "v2 dte0 CR price (mult)");
            AssertClose(5716.29, s.Dte0PutSup.Price, 0.1, "v2 dte0 PS price (mult)");
            AssertClose(5788.16, s.Dte0Hvl.Price, 0.5, "v2 dte0 HVL price (mult)");

            // Expected-move band (DynamicMultiplier -> futures_mult variant).
            Assert(s.ExpMoveHigh.HasValue, "v2 expected move high has value");
            Assert(s.ExpMoveLow.HasValue, "v2 expected move low has value");
            AssertClose(5845.013, s.ExpMoveHigh.Value, 0.1, "v2 EM high (futures_mult)");

            // RawETFStrike: everything drops to index/ETF scale.
            var raw = Parser.Parse(v2, FFGEXMappingMode.RawETFStrike);
            AssertClose(590.0, raw.CallWall.Price, 0.001, "v2 blended CW price (raw)");
            AssertClose(590.0, raw.Dte0CallRes.Price, 0.001, "v2 dte0 CR price (raw)");
            AssertClose(587.948, raw.ExpMoveHigh.Value, 0.01, "v2 EM high (raw etf)");
        }

        Console.WriteLine("\n== v1 back-compat (flat, no blended) ==");
        {
            // The original v1 fixture must STILL parse via the root fallback,
            // and must NOT yield dte0 / expected-move levels.
            var s = Parser.Parse(json, FFGEXMappingMode.DynamicMultiplier);
            Assert(s.CallWall != null, "v1 flat call wall still parses");
            AssertClose(5865.41, s.CallWall.Price, 0.1, "v1 flat CW price (mult)");
            Assert(s.Dte0CallRes == null, "v1 has no dte0 levels");
            Assert(!s.ExpMoveHigh.HasValue, "v1 has no expected move");
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
