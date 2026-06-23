#region Using declarations
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Media;
using System.Xml.Serialization;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.Gui;
using NinjaTrader.Gui.Chart;
using NinjaTrader.Gui.Tools;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.DrawingTools;
using Newtonsoft.Json.Linq;
#endregion

// FF GEX Levels — Gamma Exposure horizontal levels from the FF GEX cloud service.
//
// Architecture:
//   - On State.DataLoaded the indicator loads a local cache file (instant paint
//     with last-known-good levels) and starts a background timer.
//   - The timer fires every PollMinutes; an async HTTP GET pulls the latest
//     levels from the Worker. Network I/O is always off the UI thread.
//   - After parsing, the indicator marshals back to the UI thread via
//     ChartControl.Dispatcher.InvokeAsync to update Draw.HorizontalLine objects.
//   - Status banner is rendered via Draw.TextFixed (no SharpDX in v1 — keeps
//     the indicator simple).
//
// Threading rules followed:
//   - Static HttpClient (no per-call socket exhaustion)
//   - HTTP work inside Task.Run, never on OnBarUpdate
//   - All chart mutations marshalled to Dispatcher
//   - Timer is disposed in State.Terminated
namespace NinjaTrader.NinjaScript.Indicators
{
    public class FFGEXLevels : Indicator
    {
        #region Shared static HTTP client
        // One HttpClient for the AppDomain to avoid socket exhaustion.
        private static readonly HttpClient SharedHttp = CreateHttpClient();

        private static HttpClient CreateHttpClient()
        {
            var c = new HttpClient { Timeout = TimeSpan.FromSeconds(15) };
            c.DefaultRequestHeaders.UserAgent.ParseAdd("FFGEXLevels-NT8/1.0");
            return c;
        }
        #endregion

        #region Enums
        public enum FFGEXTickerMode
        {
            Auto, SPY, QQQ, IWM, DIA, GLD, USO
        }

        public enum FFGEXMappingMode
        {
            DynamicMultiplier,
            CarryBasis,
            RawETFStrike
        }
        #endregion

        #region Inner snapshot model
        private class FFGexSnapshot
        {
            public DateTime GeneratedAtUtc;
            public string Ticker = string.Empty;
            public double Spot;
            public double Multiplier;
            public double? Flip;
            public Level CallWall;
            public Level PutWall;
            public List<Level> PosClusters = new List<Level>();
            public List<Level> NegClusters = new List<Level>();
            public List<Level> OIClusters = new List<Level>();
            public List<string> Warnings = new List<string>();
            public int ContractCount;
            public string Status = string.Empty;

            public class Level
            {
                public double Price;          // mapped price under chosen MappingMode
                public double EtfStrike;
                public double Magnitude;      // |gex_dollars| for GEX levels, OI for OI levels
                public bool IsOI;
            }
        }
        #endregion

        #region Fields
        private CancellationTokenSource cts;
        private System.Threading.Timer pollTimer;
        private readonly object snapLock = new object();
        private FFGexSnapshot currentSnapshot;
        private string cacheFilePath;
        private string resolvedTicker = "SPY";
        private readonly HashSet<string> trackedTags = new HashSet<string>();

        // Re-entry guard for the async fetch
        private int fetchInFlight;
        #endregion

        #region NinjaScript Properties — Data
        [NinjaScriptProperty]
        [Display(Name = "Ticker", GroupName = "1. Data", Order = 1,
            Description = "Auto = infer from chart instrument (ES→SPY, NQ→QQQ, etc.)")]
        public FFGEXTickerMode Ticker { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Service URL", GroupName = "1. Data", Order = 2,
            Description = "Base URL of the Cloudflare Worker, e.g. https://ff-gex.YOURSUBDOMAIN.workers.dev")]
        public string ServiceUrl { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "API Key", GroupName = "1. Data", Order = 3,
            Description = "X-Api-Key shared secret for the Worker")]
        public string ApiKey { get; set; }

        [NinjaScriptProperty]
        [Range(1, 60)]
        [Display(Name = "Poll Interval (min)", GroupName = "1. Data", Order = 4)]
        public int PollMinutes { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Futures Mapping", GroupName = "1. Data", Order = 5,
            Description = "DynamicMultiplier (default) | CarryBasis | RawETFStrike")]
        public FFGEXMappingMode MappingMode { get; set; }
        #endregion

        #region NinjaScript Properties — Display
        [NinjaScriptProperty]
        [Display(Name = "Show Gamma Flip", GroupName = "2. Display", Order = 10)]
        public bool ShowFlip { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Show Call Wall", GroupName = "2. Display", Order = 11)]
        public bool ShowCallWall { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Show Put Wall", GroupName = "2. Display", Order = 12)]
        public bool ShowPutWall { get; set; }

        [NinjaScriptProperty]
        [Range(0, 5)]
        [Display(Name = "GEX Clusters Per Side", GroupName = "2. Display", Order = 13)]
        public int NumClusters { get; set; }

        [NinjaScriptProperty]
        [Range(0, 5)]
        [Display(Name = "OI Clusters", GroupName = "2. Display", Order = 14)]
        public int NumOIClusters { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Show Labels", GroupName = "2. Display", Order = 15)]
        public bool ShowLabels { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Show Status Banner", GroupName = "2. Display", Order = 16)]
        public bool ShowStatusBanner { get; set; }

        [NinjaScriptProperty]
        [Range(1, 48)]
        [Display(Name = "Stale Warning Hours", GroupName = "2. Display", Order = 17)]
        public int StaleHours { get; set; }
        #endregion

        #region NinjaScript Properties — Colors
        // Brushes must use the XmlIgnore pattern + a string serializer
        // for workspace persistence.

        [XmlIgnore]
        [Display(Name = "Flip Color", GroupName = "3. Colors", Order = 30)]
        public Brush FlipColor { get; set; }
        [Browsable(false)]
        public string FlipColorSerialize
        {
            get { return Serialize.BrushToString(FlipColor); }
            set { FlipColor = Serialize.StringToBrush(value); }
        }

        [XmlIgnore]
        [Display(Name = "Call Wall Color", GroupName = "3. Colors", Order = 31)]
        public Brush CallWallColor { get; set; }
        [Browsable(false)]
        public string CallWallColorSerialize
        {
            get { return Serialize.BrushToString(CallWallColor); }
            set { CallWallColor = Serialize.StringToBrush(value); }
        }

        [XmlIgnore]
        [Display(Name = "Put Wall Color", GroupName = "3. Colors", Order = 32)]
        public Brush PutWallColor { get; set; }
        [Browsable(false)]
        public string PutWallColorSerialize
        {
            get { return Serialize.BrushToString(PutWallColor); }
            set { PutWallColor = Serialize.StringToBrush(value); }
        }

        [XmlIgnore]
        [Display(Name = "+GEX Cluster Color", GroupName = "3. Colors", Order = 33)]
        public Brush PosClusterColor { get; set; }
        [Browsable(false)]
        public string PosClusterColorSerialize
        {
            get { return Serialize.BrushToString(PosClusterColor); }
            set { PosClusterColor = Serialize.StringToBrush(value); }
        }

        [XmlIgnore]
        [Display(Name = "-GEX Cluster Color", GroupName = "3. Colors", Order = 34)]
        public Brush NegClusterColor { get; set; }
        [Browsable(false)]
        public string NegClusterColorSerialize
        {
            get { return Serialize.BrushToString(NegClusterColor); }
            set { NegClusterColor = Serialize.StringToBrush(value); }
        }

        [XmlIgnore]
        [Display(Name = "OI Cluster Color", GroupName = "3. Colors", Order = 35)]
        public Brush OIClusterColor { get; set; }
        [Browsable(false)]
        public string OIClusterColorSerialize
        {
            get { return Serialize.BrushToString(OIClusterColor); }
            set { OIClusterColor = Serialize.StringToBrush(value); }
        }
        #endregion

        #region State Machine
        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Name = "FF GEX Levels";
                Description = "FlowForged Gamma Exposure horizontal levels (CBOE-sourced, dealer-positioning convention)";
                Calculate = Calculate.OnBarClose;
                IsOverlay = true;
                DisplayInDataBox = false;
                DrawOnPricePanel = true;
                IsSuspendedWhileInactive = false;
                PaintPriceMarkers = false;

                // Defaults
                Ticker = FFGEXTickerMode.Auto;
                ServiceUrl = "https://ff-gex.YOUR_SUBDOMAIN.workers.dev";
                ApiKey = "";
                PollMinutes = 5;
                MappingMode = FFGEXMappingMode.DynamicMultiplier;
                ShowFlip = true;
                ShowCallWall = true;
                ShowPutWall = true;
                NumClusters = 3;
                NumOIClusters = 2;
                ShowLabels = true;
                ShowStatusBanner = true;
                StaleHours = 6;

                // Muted FlowForged palette
                FlipColor      = new SolidColorBrush(Color.FromRgb(0xC9, 0xA4, 0x49));   // gold
                CallWallColor  = new SolidColorBrush(Color.FromRgb(0xB8, 0x50, 0x4C));   // brick red
                PutWallColor   = new SolidColorBrush(Color.FromRgb(0x4A, 0x9D, 0x6C));   // forest green
                PosClusterColor = new SolidColorBrush(Color.FromRgb(0x8A, 0x3B, 0x39));  // muted red
                NegClusterColor = new SolidColorBrush(Color.FromRgb(0x2F, 0x6A, 0x4A));  // muted green
                OIClusterColor  = new SolidColorBrush(Color.FromRgb(0x5A, 0x78, 0x96));  // steel blue

                // Freeze brushes for cross-thread use
                FreezeBrush(FlipColor); FreezeBrush(CallWallColor); FreezeBrush(PutWallColor);
                FreezeBrush(PosClusterColor); FreezeBrush(NegClusterColor); FreezeBrush(OIClusterColor);
            }
            else if (State == State.Configure)
            {
                ResolveTickerFromInstrument();
                cacheFilePath = BuildCachePath(resolvedTicker);
            }
            else if (State == State.DataLoaded)
            {
                cts = new CancellationTokenSource();
                // Paint initial banner so users see "connecting..." right away,
                // even before the cache load or first fetch completes.
                if (ChartControl != null && ShowStatusBanner)
                {
                    ChartControl.Dispatcher.InvokeAsync(() => {
                        try { DrawStatusBanner(null); ForceRefresh(); } catch { /* ignore */ }
                    });
                }
                LoadCacheFromDisk();   // instant paint with last-known-good
                _ = FetchFromCloudAsync();
                pollTimer = new System.Threading.Timer(
                    _ => _ = FetchFromCloudAsync(),
                    null,
                    TimeSpan.FromMinutes(PollMinutes),
                    TimeSpan.FromMinutes(PollMinutes));
            }
            else if (State == State.Terminated)
            {
                try { cts?.Cancel(); } catch { /* ignore */ }
                try { pollTimer?.Dispose(); } catch { /* ignore */ }
            }
        }

        private static void FreezeBrush(Brush b)
        {
            if (b != null && b.CanFreeze && !b.IsFrozen)
                b.Freeze();
        }

        protected override void OnBarUpdate()
        {
            // Intentionally empty — levels are fetched on a timer, not per-bar.
        }
        #endregion

        #region Ticker resolution
        private void ResolveTickerFromInstrument()
        {
            if (Ticker != FFGEXTickerMode.Auto)
            {
                resolvedTicker = Ticker.ToString();
                return;
            }

            // Infer from chart instrument's master name (root symbol).
            string master = (Instrument?.MasterInstrument?.Name ?? "").ToUpperInvariant();
            switch (master)
            {
                case "ES":   resolvedTicker = "SPY"; break;
                case "NQ":   resolvedTicker = "QQQ"; break;
                case "RTY":  resolvedTicker = "IWM"; break;
                case "YM":   resolvedTicker = "DIA"; break;
                case "GC":   resolvedTicker = "GLD"; break;
                case "CL":   resolvedTicker = "USO"; break;
                default:
                    resolvedTicker = "SPY";
                    Log($"FF GEX Levels: Auto mode could not map '{master}' — defaulting to SPY", LogLevel.Warning);
                    break;
            }
        }
        #endregion

        #region Cache path
        private string BuildCachePath(string ticker)
        {
            string dir = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments),
                "NinjaTrader 8", "FFGEX");
            try { Directory.CreateDirectory(dir); } catch { /* ignore */ }
            return Path.Combine(dir, $"cache_{ticker.ToUpperInvariant()}.json");
        }
        #endregion

        #region Async cloud fetch
        private async Task FetchFromCloudAsync()
        {
            // Prevent overlapping fetches
            if (Interlocked.Exchange(ref fetchInFlight, 1) == 1) return;
            try
            {
                if (cts == null || cts.IsCancellationRequested) return;
                if (string.IsNullOrWhiteSpace(ServiceUrl) || string.IsNullOrWhiteSpace(ApiKey))
                {
                    Log("FF GEX Levels: ServiceUrl and ApiKey must be configured", LogLevel.Warning);
                    return;
                }

                string url = ServiceUrl.TrimEnd('/') + "/gex/" + resolvedTicker;
                var req = new HttpRequestMessage(HttpMethod.Get, url);
                req.Headers.Add("X-Api-Key", ApiKey);

                HttpResponseMessage resp;
                try
                {
                    resp = await SharedHttp.SendAsync(req, cts.Token);
                }
                catch (TaskCanceledException)
                {
                    return;
                }
                catch (Exception ex)
                {
                    Log($"FF GEX Levels: HTTP error {ex.Message}", LogLevel.Warning);
                    return;
                }

                if (!resp.IsSuccessStatusCode)
                {
                    Log($"FF GEX Levels: HTTP {(int)resp.StatusCode} from {url}", LogLevel.Warning);
                    return;
                }

                string body = await resp.Content.ReadAsStringAsync();
                FFGexSnapshot snap;
                try
                {
                    snap = ParseSnapshot(body, MappingMode);
                }
                catch (Exception ex)
                {
                    Log($"FF GEX Levels: parse error {ex.Message}", LogLevel.Warning);
                    return;
                }

                // Persist to disk (best-effort)
                try { File.WriteAllText(cacheFilePath, body); } catch { /* ignore */ }

                lock (snapLock) currentSnapshot = snap;

                if (ChartControl != null)
                {
                    ChartControl.Dispatcher.InvokeAsync(() =>
                    {
                        try
                        {
                            RedrawAllLevels(snap);
                            ForceRefresh();
                        }
                        catch (Exception ex)
                        {
                            Log($"FF GEX Levels: redraw failed {ex.Message}", LogLevel.Error);
                        }
                    });
                }
            }
            finally
            {
                Interlocked.Exchange(ref fetchInFlight, 0);
            }
        }
        #endregion

        #region Cache load
        private void LoadCacheFromDisk()
        {
            try
            {
                if (!File.Exists(cacheFilePath)) return;
                string body = File.ReadAllText(cacheFilePath);
                var snap = ParseSnapshot(body, MappingMode);
                lock (snapLock) currentSnapshot = snap;
                if (ChartControl != null)
                {
                    ChartControl.Dispatcher.InvokeAsync(() =>
                    {
                        try { RedrawAllLevels(snap); } catch { /* ignore */ }
                    });
                }
            }
            catch (Exception ex)
            {
                Log($"FF GEX Levels: cache load failed {ex.Message}", LogLevel.Warning);
            }
        }
        #endregion

        #region Parsing
        private static FFGexSnapshot ParseSnapshot(string json, FFGEXMappingMode mode)
        {
            var root = JObject.Parse(json);
            var snap = new FFGexSnapshot();
            snap.Ticker = (string)root["ticker"] ?? "";
            snap.Status = (string)root["status"] ?? "";
            snap.GeneratedAtUtc = ParseUtc((string)root["generated_at"]);

            var spotTok = root["spot"];
            if (spotTok != null && spotTok.Type != JTokenType.Null)
                snap.Spot = (double)spotTok;
            var multTok = root["multiplier"];
            if (multTok != null && multTok.Type != JTokenType.Null)
                snap.Multiplier = (double)multTok;

            var ccTok = root["contract_count"];
            if (ccTok != null && ccTok.Type != JTokenType.Null)
                snap.ContractCount = (int)ccTok;

            var warnings = root["warnings"] as JArray;
            if (warnings != null)
                foreach (var w in warnings) snap.Warnings.Add((string)w);

            if (snap.Status != "ok") return snap;

            // Flip
            var flipNode = root["gamma_flip"];
            if (flipNode != null && flipNode.Type != JTokenType.Null)
            {
                double? p = MapPrice(flipNode, mode);
                if (p.HasValue) snap.Flip = p;
            }

            // Call wall
            var cwNode = root["call_wall"];
            if (cwNode != null && cwNode.Type != JTokenType.Null)
            {
                var lvl = NodeToLevel(cwNode, mode, isOI: false);
                if (lvl != null) snap.CallWall = lvl;
            }

            // Put wall
            var pwNode = root["put_wall"];
            if (pwNode != null && pwNode.Type != JTokenType.Null)
            {
                var lvl = NodeToLevel(pwNode, mode, isOI: false);
                if (lvl != null) snap.PutWall = lvl;
            }

            // Clusters
            AppendClusters(root["top_pos_clusters"], snap.PosClusters, mode, isOI: false);
            AppendClusters(root["top_neg_clusters"], snap.NegClusters, mode, isOI: false);
            AppendClusters(root["top_oi_clusters"], snap.OIClusters, mode, isOI: true);

            return snap;
        }

        private static void AppendClusters(JToken arr, List<FFGexSnapshot.Level> sink,
            FFGEXMappingMode mode, bool isOI)
        {
            var ja = arr as JArray;
            if (ja == null) return;
            foreach (var item in ja)
            {
                var lvl = NodeToLevel(item, mode, isOI);
                if (lvl != null) sink.Add(lvl);
            }
        }

        private static FFGexSnapshot.Level NodeToLevel(JToken node, FFGEXMappingMode mode, bool isOI)
        {
            double? p = MapPrice(node, mode);
            if (!p.HasValue) return null;
            var lvl = new FFGexSnapshot.Level
            {
                Price = p.Value,
                EtfStrike = (double?)node["etf_strike"] ?? 0,
                IsOI = isOI,
                Magnitude = isOI
                    ? Math.Abs((double?)node["open_interest"] ?? 0)
                    : Math.Abs((double?)node["gex_dollars"] ?? 0),
            };
            return lvl;
        }

        private static double? MapPrice(JToken node, FFGEXMappingMode mode)
        {
            if (node == null || node.Type == JTokenType.Null) return null;
            JToken tok = null;
            switch (mode)
            {
                case FFGEXMappingMode.DynamicMultiplier: tok = node["futures_mult"]; break;
                case FFGEXMappingMode.CarryBasis:        tok = node["futures_basis"]; break;
                case FFGEXMappingMode.RawETFStrike:      tok = node["etf_strike"]; break;
            }
            if (tok == null || tok.Type == JTokenType.Null) return null;
            return (double)tok;
        }

        private static DateTime ParseUtc(string iso)
        {
            if (string.IsNullOrEmpty(iso)) return DateTime.MinValue;
            DateTime parsed;
            if (DateTime.TryParse(iso, System.Globalization.CultureInfo.InvariantCulture,
                System.Globalization.DateTimeStyles.AdjustToUniversal | System.Globalization.DateTimeStyles.AssumeUniversal,
                out parsed))
            {
                return parsed.ToUniversalTime();
            }
            return DateTime.MinValue;
        }
        #endregion

        #region Drawing
        private void RedrawAllLevels(FFGexSnapshot s)
        {
            // Remove anything we drew last time
            foreach (var tag in trackedTags.ToArray())
                RemoveDrawObject(tag);
            trackedTags.Clear();

            // Status banner first
            if (ShowStatusBanner)
                DrawStatusBanner(s);

            if (s == null || s.Status != "ok") return;

            // Compute max |GEX| for opacity/width scaling (walls always max)
            double maxAbsGex = 0;
            if (s.CallWall != null) maxAbsGex = Math.Max(maxAbsGex, s.CallWall.Magnitude);
            if (s.PutWall != null) maxAbsGex = Math.Max(maxAbsGex, s.PutWall.Magnitude);
            if (maxAbsGex <= 0) maxAbsGex = 1;

            // Flip
            if (ShowFlip && s.Flip.HasValue)
            {
                DrawLevel("FFGEX_Flip", s.Flip.Value, FlipColor,
                    DashStyleHelper.Dash, 2, 1.0,
                    string.Format("FLIP {0:F2}", s.Flip.Value));
            }

            // Call wall
            if (ShowCallWall && s.CallWall != null)
            {
                string label = string.Format("CW {0:F2}  {1}", s.CallWall.Price,
                    FormatBillions(s.CallWall.Magnitude));
                DrawLevel("FFGEX_CW", s.CallWall.Price, CallWallColor,
                    DashStyleHelper.Solid, 3, 1.0, label);
            }

            // Put wall
            if (ShowPutWall && s.PutWall != null)
            {
                string label = string.Format("PW {0:F2}  {1}", s.PutWall.Price,
                    FormatBillions(s.PutWall.Magnitude));
                DrawLevel("FFGEX_PW", s.PutWall.Price, PutWallColor,
                    DashStyleHelper.Solid, 3, 1.0, label);
            }

            // Positive clusters
            int npc = Math.Min(NumClusters, s.PosClusters.Count);
            for (int i = 0; i < npc; i++)
            {
                var c = s.PosClusters[i];
                int width = WidthForMagnitude(c.Magnitude, maxAbsGex);
                double opacity = OpacityForMagnitude(c.Magnitude, maxAbsGex);
                DrawLevel($"FFGEX_PC_{i}", c.Price, PosClusterColor,
                    DashStyleHelper.Solid, width, opacity,
                    ShowLabels ? string.Format("+G {0:F2}", c.Price) : null);
            }

            // Negative clusters
            int nnc = Math.Min(NumClusters, s.NegClusters.Count);
            for (int i = 0; i < nnc; i++)
            {
                var c = s.NegClusters[i];
                int width = WidthForMagnitude(c.Magnitude, maxAbsGex);
                double opacity = OpacityForMagnitude(c.Magnitude, maxAbsGex);
                DrawLevel($"FFGEX_NC_{i}", c.Price, NegClusterColor,
                    DashStyleHelper.Solid, width, opacity,
                    ShowLabels ? string.Format("-G {0:F2}", c.Price) : null);
            }

            // OI clusters
            int noi = Math.Min(NumOIClusters, s.OIClusters.Count);
            for (int i = 0; i < noi; i++)
            {
                var c = s.OIClusters[i];
                DrawLevel($"FFGEX_OI_{i}", c.Price, OIClusterColor,
                    DashStyleHelper.DashDot, 1, 0.6,
                    ShowLabels ? string.Format("OI {0:F2}", c.Price) : null);
            }
        }

        private void DrawLevel(string tag, double price, Brush color,
            DashStyleHelper dash, int width, double opacity, string label)
        {
            // Use a per-instance brush at the requested opacity to encode
            // magnitude. The brush is built fresh each redraw; we freeze it
            // so the chart pipeline can use it cross-thread.
            Brush effective = ScaleBrushOpacity(color, opacity);

            Draw.HorizontalLine(this, tag, price, effective, dash, width);
            trackedTags.Add(tag);

            if (ShowLabels && !string.IsNullOrEmpty(label))
            {
                string ltag = tag + "_lbl";
                // Render label at right edge using a Text drawing object.
                // The X coordinate is the most recent bar's time; placement at
                // the actual right margin is handled by NT8's draw helpers.
                Draw.Text(this, ltag, false, label, 0, price, 4, effective,
                    new SimpleFont("Arial", 10) { Bold = true },
                    System.Windows.Media.TextAlignment.Right,
                    Brushes.Transparent, Brushes.Transparent, 0);
                trackedTags.Add(ltag);
            }
        }

        private static Brush ScaleBrushOpacity(Brush source, double opacity)
        {
            if (opacity >= 0.99) return source;
            var solid = source as SolidColorBrush;
            if (solid == null) return source;
            var c = solid.Color;
            byte a = (byte)Math.Round(Math.Max(0, Math.Min(1, opacity)) * 255);
            var scaled = new SolidColorBrush(Color.FromArgb(a, c.R, c.G, c.B));
            if (scaled.CanFreeze) scaled.Freeze();
            return scaled;
        }

        private static int WidthForMagnitude(double mag, double maxAbs)
        {
            double r = Math.Min(1.0, mag / Math.Max(1e-9, maxAbs));
            int w = (int)Math.Round(1.0 + r * 1.0);   // 1..2
            return Math.Max(1, w);
        }

        private static double OpacityForMagnitude(double mag, double maxAbs)
        {
            double r = Math.Min(1.0, mag / Math.Max(1e-9, maxAbs));
            return 0.35 + r * 0.60;
        }

        private static string FormatBillions(double dollars)
        {
            double bn = Math.Abs(dollars) / 1e9;
            if (bn >= 1) return string.Format("${0:F1}B", bn);
            double mm = Math.Abs(dollars) / 1e6;
            return string.Format("${0:F0}M", mm);
        }
        #endregion

        #region Status banner
        private void DrawStatusBanner(FFGexSnapshot s)
        {
            const string tag = "FFGEX_Status";
            string text;
            Brush color;
            if (s == null)
            {
                text = $"FF GEX [{resolvedTicker}] connecting…";
                color = Brushes.Gray;
            }
            else if (s.Status != "ok")
            {
                text = $"FF GEX [{resolvedTicker}] ERROR";
                color = Brushes.OrangeRed;
            }
            else
            {
                var age = DateTime.UtcNow - s.GeneratedAtUtc;
                bool stale = age.TotalHours > StaleHours;
                string ageStr = age.TotalHours >= 1
                    ? string.Format("{0:F1}h ago", age.TotalHours)
                    : string.Format("{0:F0}m ago", Math.Max(0, age.TotalMinutes));
                text = stale
                    ? $"FF GEX [{resolvedTicker}] STALE {ageStr}"
                    : $"FF GEX [{resolvedTicker}] {ageStr}";
                color = stale ? Brushes.Orange : Brushes.Gray;
            }

            Draw.TextFixed(this, tag, text, TextPosition.TopLeft, color,
                new SimpleFont("Arial", 10) { Bold = true },
                Brushes.Transparent, Brushes.Transparent, 0);
            trackedTags.Add(tag);
        }
        #endregion

        #region Logging helper
        private void Log(string msg, LogLevel level = LogLevel.Information)
        {
            try { Print($"[{DateTime.UtcNow:HH:mm:ss}] {msg}"); } catch { /* ignore */ }
        }
        #endregion
    }
}

#region NinjaScript generated code. Neither change nor remove.

namespace NinjaTrader.NinjaScript.Indicators
{
    public partial class Indicator : NinjaTrader.Gui.NinjaScript.IndicatorRenderBase
    {
        private FFGEXLevels[] cacheFFGEXLevels;
        public FFGEXLevels FFGEXLevels(FFGEXLevels.FFGEXTickerMode ticker, string serviceUrl, string apiKey,
            int pollMinutes, FFGEXLevels.FFGEXMappingMode mappingMode, bool showFlip, bool showCallWall,
            bool showPutWall, int numClusters, int numOIClusters, bool showLabels, bool showStatusBanner,
            int staleHours)
        {
            return FFGEXLevels(Input, ticker, serviceUrl, apiKey, pollMinutes, mappingMode, showFlip,
                showCallWall, showPutWall, numClusters, numOIClusters, showLabels, showStatusBanner, staleHours);
        }

        public FFGEXLevels FFGEXLevels(ISeries<double> input, FFGEXLevels.FFGEXTickerMode ticker,
            string serviceUrl, string apiKey, int pollMinutes, FFGEXLevels.FFGEXMappingMode mappingMode,
            bool showFlip, bool showCallWall, bool showPutWall, int numClusters, int numOIClusters,
            bool showLabels, bool showStatusBanner, int staleHours)
        {
            if (cacheFFGEXLevels != null)
                for (int idx = 0; idx < cacheFFGEXLevels.Length; idx++)
                    if (cacheFFGEXLevels[idx] != null && cacheFFGEXLevels[idx].Ticker == ticker
                        && cacheFFGEXLevels[idx].ServiceUrl == serviceUrl
                        && cacheFFGEXLevels[idx].ApiKey == apiKey
                        && cacheFFGEXLevels[idx].PollMinutes == pollMinutes
                        && cacheFFGEXLevels[idx].MappingMode == mappingMode
                        && cacheFFGEXLevels[idx].ShowFlip == showFlip
                        && cacheFFGEXLevels[idx].ShowCallWall == showCallWall
                        && cacheFFGEXLevels[idx].ShowPutWall == showPutWall
                        && cacheFFGEXLevels[idx].NumClusters == numClusters
                        && cacheFFGEXLevels[idx].NumOIClusters == numOIClusters
                        && cacheFFGEXLevels[idx].ShowLabels == showLabels
                        && cacheFFGEXLevels[idx].ShowStatusBanner == showStatusBanner
                        && cacheFFGEXLevels[idx].StaleHours == staleHours
                        && cacheFFGEXLevels[idx].EqualsInput(input))
                        return cacheFFGEXLevels[idx];
            return CacheIndicator<FFGEXLevels>(new FFGEXLevels()
            {
                Ticker = ticker, ServiceUrl = serviceUrl, ApiKey = apiKey,
                PollMinutes = pollMinutes, MappingMode = mappingMode,
                ShowFlip = showFlip, ShowCallWall = showCallWall, ShowPutWall = showPutWall,
                NumClusters = numClusters, NumOIClusters = numOIClusters,
                ShowLabels = showLabels, ShowStatusBanner = showStatusBanner,
                StaleHours = staleHours
            }, input, ref cacheFFGEXLevels);
        }
    }
}

namespace NinjaTrader.NinjaScript.MarketAnalyzerColumns
{
    public partial class MarketAnalyzerColumn : MarketAnalyzerColumnBase
    {
        public Indicators.FFGEXLevels FFGEXLevels(Indicators.FFGEXLevels.FFGEXTickerMode ticker,
            string serviceUrl, string apiKey, int pollMinutes,
            Indicators.FFGEXLevels.FFGEXMappingMode mappingMode, bool showFlip, bool showCallWall,
            bool showPutWall, int numClusters, int numOIClusters, bool showLabels,
            bool showStatusBanner, int staleHours)
        {
            return indicator.FFGEXLevels(Input, ticker, serviceUrl, apiKey, pollMinutes, mappingMode,
                showFlip, showCallWall, showPutWall, numClusters, numOIClusters, showLabels,
                showStatusBanner, staleHours);
        }
    }
}

namespace NinjaTrader.NinjaScript.Strategies
{
    public partial class Strategy : NinjaTrader.Gui.NinjaScript.StrategyRenderBase
    {
        public Indicators.FFGEXLevels FFGEXLevels(Indicators.FFGEXLevels.FFGEXTickerMode ticker,
            string serviceUrl, string apiKey, int pollMinutes,
            Indicators.FFGEXLevels.FFGEXMappingMode mappingMode, bool showFlip, bool showCallWall,
            bool showPutWall, int numClusters, int numOIClusters, bool showLabels,
            bool showStatusBanner, int staleHours)
        {
            return indicator.FFGEXLevels(Input, ticker, serviceUrl, apiKey, pollMinutes, mappingMode,
                showFlip, showCallWall, showPutWall, numClusters, numOIClusters, showLabels,
                showStatusBanner, staleHours);
        }
    }
}

#endregion
