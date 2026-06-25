// Standalone validation of the parsing logic from FFGEXLevels.cs.
// Mirrors ParseSnapshot EXACTLY, including the dependency-free MiniJson parser
// the indicator uses (NinjaScript has no Newtonsoft.Json reference by default,
// so the indicator hand-rolls a tiny JSON parser using only core BCL types).
// Written in conservative C# (no pattern-matching / out-var) so this file and
// the NinjaScript file share identical logic.

using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;

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
        var s = new Snapshot();
        var root = MiniJson.Parse(json) as Dictionary<string, object>;
        if (root == null) return s;

        s.Ticker = Str(Get(root, "ticker")) ?? "";
        s.Status = Str(Get(root, "status")) ?? "";
        s.GeneratedAtUtc = ParseUtc(Str(Get(root, "generated_at")));
        double? spot = Num(Get(root, "spot"));
        if (spot.HasValue) s.Spot = spot.Value;
        double? mult = Num(Get(root, "multiplier"));
        if (mult.HasValue) s.Multiplier = mult.Value;
        double? cc = Num(Get(root, "contract_count"));
        if (cc.HasValue) s.ContractCount = (int)cc.Value;
        List<object> warns = Arr(Get(root, "warnings"));
        if (warns != null)
            foreach (var w in warns) { string ws = Str(w); if (ws != null) s.Warnings.Add(ws); }

        if (s.Status != "ok") return s;

        // v2 nests the structural levels under "blended"; v1 had them at root.
        object blended = Get(root, "blended");
        object levels = IsObj(blended) ? blended : root;

        double? flip = MapPrice(Get(levels, "gamma_flip"), mode);
        if (flip.HasValue) s.Flip = flip;
        s.CallWall = NodeToLevel(Get(levels, "call_wall"), mode, false);
        s.PutWall = NodeToLevel(Get(levels, "put_wall"), mode, false);
        AppendClusters(Arr(Get(levels, "top_pos_clusters")), s.PosClusters, mode, false);
        AppendClusters(Arr(Get(levels, "top_neg_clusters")), s.NegClusters, mode, false);
        AppendClusters(Arr(Get(levels, "top_oi_clusters")), s.OIClusters, mode, true);

        // v2 0DTE block (null when no same-day expiry; sides may be null)
        object dte0 = Get(root, "dte0");
        if (IsObj(dte0))
        {
            s.Dte0CallRes = NodeToLevel(Get(dte0, "call_resistance_0dte"), mode, false);
            s.Dte0PutSup = NodeToLevel(Get(dte0, "put_support_0dte"), mode, false);
            s.Dte0Hvl = NodeToLevel(Get(dte0, "hvl_0dte"), mode, false);
            s.Dte0GammaWall = NodeToLevel(Get(dte0, "gamma_wall_0dte"), mode, false);
        }

        // v2 1-day expected-move band
        object em = Get(root, "expected_move_1d");
        if (IsObj(em))
        {
            s.ExpMoveHigh = MapMovePrice(em, mode, true);
            s.ExpMoveLow = MapMovePrice(em, mode, false);
        }

        return s;
    }

    static void AppendClusters(List<object> arr, List<Snapshot.Level> sink,
        FFGEXMappingMode mode, bool isOI)
    {
        if (arr == null) return;
        foreach (var item in arr)
        {
            var lvl = NodeToLevel(item, mode, isOI);
            if (lvl != null) sink.Add(lvl);
        }
    }

    static Snapshot.Level NodeToLevel(object node, FFGEXMappingMode mode, bool isOI)
    {
        double? p = MapPrice(node, mode);
        if (!p.HasValue) return null;
        return new Snapshot.Level
        {
            Price = p.Value,
            EtfStrike = Num(Get(node, "etf_strike")) ?? 0,
            IsOI = isOI,
            Magnitude = isOI
                ? Math.Abs(Num(Get(node, "open_interest")) ?? 0)
                : Math.Abs(Num(Get(node, "gex_dollars")) ?? 0),
        };
    }

    static double? MapPrice(object node, FFGEXMappingMode mode)
    {
        if (node == null) return null;
        string key = mode == FFGEXMappingMode.DynamicMultiplier ? "futures_mult"
                   : mode == FFGEXMappingMode.CarryBasis ? "futures_basis"
                   : "etf_strike";
        return Num(Get(node, key));
    }

    static double? MapMovePrice(object em, FFGEXMappingMode mode, bool high)
    {
        string key = mode == FFGEXMappingMode.RawETFStrike
            ? (high ? "high_etf" : "low_etf")
            : (high ? "high_futures_mult" : "low_futures_mult");
        return Num(Get(em, key));
    }

    // ---- object-model accessors over MiniJson output ----
    static object Get(object node, string key)
    {
        var d = node as Dictionary<string, object>;
        object v;
        return (d != null && d.TryGetValue(key, out v)) ? v : null;
    }
    static string Str(object node) { return node as string; }
    static double? Num(object node) { if (node is double) return (double)node; return null; }
    static List<object> Arr(object node) { return node as List<object>; }
    static bool IsObj(object node) { return node is Dictionary<string, object>; }

    static DateTime ParseUtc(string iso)
    {
        if (string.IsNullOrEmpty(iso)) return DateTime.MinValue;
        DateTime d;
        return DateTime.TryParse(iso, CultureInfo.InvariantCulture,
            DateTimeStyles.AdjustToUniversal | DateTimeStyles.AssumeUniversal, out d)
            ? d.ToUniversalTime() : DateTime.MinValue;
    }
}

// Minimal dependency-free JSON parser. Objects -> Dictionary<string,object>,
// arrays -> List<object>, numbers -> double, plus string / bool / null.
// Uses only System.Collections.Generic, System.Text, System.Globalization.
internal static class MiniJson
{
    public static object Parse(string s)
    {
        if (string.IsNullOrEmpty(s)) return null;
        int i = 0;
        return ParseValue(s, ref i);
    }

    static object ParseValue(string s, ref int i)
    {
        SkipWs(s, ref i);
        if (i >= s.Length) return null;
        char c = s[i];
        if (c == '{') return ParseObject(s, ref i);
        if (c == '[') return ParseArray(s, ref i);
        if (c == '"') return ParseString(s, ref i);
        if (c == 't') { i += 4; return true; }
        if (c == 'f') { i += 5; return false; }
        if (c == 'n') { i += 4; return null; }
        return ParseNumber(s, ref i);
    }

    static Dictionary<string, object> ParseObject(string s, ref int i)
    {
        var d = new Dictionary<string, object>();
        i++; // {
        SkipWs(s, ref i);
        if (i < s.Length && s[i] == '}') { i++; return d; }
        while (i < s.Length)
        {
            SkipWs(s, ref i);
            string key = ParseString(s, ref i);
            SkipWs(s, ref i);
            if (i < s.Length && s[i] == ':') i++;
            object val = ParseValue(s, ref i);
            d[key] = val;
            SkipWs(s, ref i);
            if (i < s.Length && s[i] == ',') { i++; continue; }
            if (i < s.Length && s[i] == '}') { i++; break; }
            break;
        }
        return d;
    }

    static List<object> ParseArray(string s, ref int i)
    {
        var list = new List<object>();
        i++; // [
        SkipWs(s, ref i);
        if (i < s.Length && s[i] == ']') { i++; return list; }
        while (i < s.Length)
        {
            object val = ParseValue(s, ref i);
            list.Add(val);
            SkipWs(s, ref i);
            if (i < s.Length && s[i] == ',') { i++; continue; }
            if (i < s.Length && s[i] == ']') { i++; break; }
            break;
        }
        return list;
    }

    static string ParseString(string s, ref int i)
    {
        var sb = new StringBuilder();
        i++; // opening quote
        while (i < s.Length)
        {
            char c = s[i++];
            if (c == '"') break;
            if (c == '\\' && i < s.Length)
            {
                char e = s[i++];
                if (e == '"') sb.Append('"');
                else if (e == '\\') sb.Append('\\');
                else if (e == '/') sb.Append('/');
                else if (e == 'b') sb.Append('\b');
                else if (e == 'f') sb.Append('\f');
                else if (e == 'n') sb.Append('\n');
                else if (e == 'r') sb.Append('\r');
                else if (e == 't') sb.Append('\t');
                else if (e == 'u' && i + 4 <= s.Length)
                {
                    int code = int.Parse(s.Substring(i, 4), NumberStyles.HexNumber, CultureInfo.InvariantCulture);
                    sb.Append((char)code);
                    i += 4;
                }
                else sb.Append(e);
            }
            else sb.Append(c);
        }
        return sb.ToString();
    }

    static object ParseNumber(string s, ref int i)
    {
        int start = i;
        while (i < s.Length)
        {
            char c = s[i];
            if ((c >= '0' && c <= '9') || c == '-' || c == '+' || c == '.' || c == 'e' || c == 'E')
                i++;
            else break;
        }
        string num = s.Substring(start, i - start);
        double d;
        if (double.TryParse(num, NumberStyles.Float, CultureInfo.InvariantCulture, out d))
            return d;
        return null;
    }

    static void SkipWs(string s, ref int i)
    {
        while (i < s.Length)
        {
            char c = s[i];
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') i++;
            else break;
        }
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
