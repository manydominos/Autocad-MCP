// McpPointCloud - point cloud verbs for autocad-mcp (fork manydominos/Autocad-MCP).
//
// One AutoLISP function, (mcp-pc "<request.json>"): reads a JSON request, does one
// operation on an attached RCS/RCP point cloud (AcDbPointCloudEx) through the
// AutoCAD .NET API, writes "<request.json>.out" with the JSON answer and returns
// "ok" or "err". No prompt, no dialog: the MCP server sends the call through
// Document.SendCommand and reads the answer file.
//
// Built against the AutoCAD 2021 (R24.0) reference assemblies (NuGet AutoCAD.NET
// 24.0.0, not redistributed) with the .NET Framework 4 csc.exe - C# 5 only.
// API verified from AcDbMgd 24.0 metadata: PointCloudEx.AttachPointCloud,
// addCroppingBoundary/clearCropping, PointCloudCrop, MinMaxElevation,
// ElevationOutOfRangeBehavior, PointCloudExtractor.Extract.

using System;
using System.Collections;
using System.Collections.Generic;
using System.IO;
using System.Web.Script.Serialization;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.Geometry;
using Autodesk.AutoCAD.Runtime;
using AcApp = Autodesk.AutoCAD.ApplicationServices.Core.Application;

[assembly: CommandClass(typeof(McpPointCloud.Api))]

namespace McpPointCloud
{
    public class Api
    {
        public const string Version = "0.1.3";

        [LispFunction("mcp-pc")]
        public object Dispatch(ResultBuffer args)
        {
            string req = null;
            if (args != null)
            {
                TypedValue[] a = args.AsArray();
                if (a.Length > 0 && a[0].Value is string) req = (string)a[0].Value;
            }
            if (req == null) return "err";
            JavaScriptSerializer js;
            string outPath = req + ".out";
            Dictionary<string, object> res;
            try
            {
                js = new JavaScriptSerializer();
                js.MaxJsonLength = int.MaxValue;
            }
            catch (System.Exception) { return "err"; }
            try
            {
                var r = js.Deserialize<Dictionary<string, object>>(File.ReadAllText(req));
                string op = Str(r, "op", "");
                if (op == "ping") res = Ping();
                else if (op == "attach") res = Attach(r);
                else if (op == "info") res = Info(r);
                else if (op == "crop") res = Crop(r);
                else if (op == "crop_clear") res = CropClear(r);
                else if (op == "style") res = Style(r);
                else if (op == "section") res = Section(r);
                else if (op == "dump") res = Dump(r);
                else res = Fail("unknown_op", "unknown op " + op);
            }
            catch (System.Exception ex)
            {
                res = Fail("exception", ex.GetType().Name + ": " + ex.Message);
            }
            try
            {
                string tmp = outPath + ".part";
                File.WriteAllText(tmp, js.Serialize(res));
                if (File.Exists(outPath)) File.Delete(outPath);
                File.Move(tmp, outPath);
            }
            catch (System.Exception)
            {
                return "err";  // the server reports the missing answer; AutoCAD keeps running
            }
            object ok;
            return res.TryGetValue("ok", out ok) && ok is bool && (bool)ok ? "ok" : "err";
        }

        // ── operations ─────────────────────────────────────────────────────

        static Dictionary<string, object> Ping()
        {
            var d = Ok();
            d["version"] = Version;
            d["acad"] = AcApp.Version.ToString();
            return d;
        }

        static Dictionary<string, object> Attach(Dictionary<string, object> r)
        {
            Document doc = AcApp.DocumentManager.MdiActiveDocument;
            Database db = doc.Database;
            string path = Str(r, "path", null);
            if (string.IsNullOrEmpty(path) || !File.Exists(path))
                return Fail("file_not_found", "no point cloud file at " + path + " (seen from AutoCAD)");
            Point3d at = Pt(r, "insert", Point3d.Origin);
            double scale = Num(r, "scale", 1.0);
            double rotation = Num(r, "rotation_deg", 0.0) * Math.PI / 180.0;
            string layer = Str(r, "layer", null);
            ObjectId id;
            using (doc.LockDocument())
            {
                id = PointCloudEx.AttachPointCloud(path, at, scale, rotation, db);
                if (id.IsNull) return Fail("attach_failed", "AttachPointCloud returned no entity for " + path);
                using (Transaction tr = db.TransactionManager.StartTransaction())
                {
                    var pc = (PointCloudEx)tr.GetObject(id, OpenMode.ForWrite);
                    if (!string.IsNullOrEmpty(layer))
                    {
                        EnsureLayer(db, tr, layer);
                        pc.Layer = layer;
                    }
                    tr.Commit();
                }
            }
            var d = Ok();
            d["cloud"] = Describe(db, id, false);
            return d;
        }

        static Dictionary<string, object> Info(Dictionary<string, object> r)
        {
            Database db = AcApp.DocumentManager.MdiActiveDocument.Database;
            bool items = Bool(r, "items", false);
            var list = new List<object>();
            string handle = Str(r, "handle", null);
            if (!string.IsNullOrEmpty(handle))
            {
                ObjectId id;
                string why = Find(db, handle, out id);
                if (why != null) return Fail("not_found", why);
                list.Add(Describe(db, id, items));
            }
            else
            {
                foreach (ObjectId id in AllClouds(db)) list.Add(Describe(db, id, items));
            }
            var d = Ok();
            d["clouds"] = list;
            d["count"] = list.Count;
            return d;
        }

        // Crop (0.1.2). Live 2026-11-24 with 0.1.1: every crop hid all points
        // (inside=true) or none (inside=false), with WCS mm and native m alike.
        // Crops AutoCAD itself made on the same cloud read back as Polygonal,
        // inside=true, vertices in NATIVE metres with z=0 and no usable plane.
        // So by default the outline is converted to the cloud's native frame
        // (inverse of Location, Rotation, Scale), z=0, closed, and no CropPlane is
        // set (plane_mode "none"); an existing crop is edited in place
        // (getPointCloudCropping is the API's "for editing" accessor) before a
        // new one is made with PointCloudCrop.Create(IntPtr.Zero). Every variant is
        // a parameter, and the answer carries what AutoCAD stored (read back).
        static Dictionary<string, object> Crop(Dictionary<string, object> r)
        {
            Document doc = AcApp.DocumentManager.MdiActiveDocument;
            Database db = doc.Database;
            ObjectId id;
            string why = Find(db, Str(r, "handle", null), out id);
            if (why != null) return Fail("not_found", why);
            var poly = List2(r, "polygon");
            if (poly.Count < 3) return Fail("invalid_args", "polygon needs at least three [x, y] points");
            string plane = Str(r, "plane", "top");
            bool inside = Bool(r, "inside", true);
            bool inverted = Bool(r, "inverted", false);
            bool replace = Bool(r, "replace", true);
            bool close = Bool(r, "close", true);
            string vertexSpace = Str(r, "vertex_space", "wcs");
            string planeMode = Str(r, "plane_mode", "none");
            string method = Str(r, "method", "auto");
            object zmin = Get(r, "zmin");
            object zmax = Get(r, "zmax");
            string zspace = Str(r, "zband_space", "wcs");
            var diag = new Dictionary<string, object>();
            using (doc.LockDocument())
            using (Transaction tr = db.TransactionManager.StartTransaction())
            {
                PointCloudEx pc;
                string lockedWhy = OpenForWrite(db, tr, id, out pc);
                if (lockedWhy != null) return Fail("layer_locked", lockedWhy);
                // WCS -> native: inverse of the cloud's own transform; "native" input
                // is stored as given; "raw_wcs" stores WCS unconverted (0.1.1 behaviour).
                Matrix3d toNative = vertexSpace == "wcs" ? CloudTransform(pc).Inverse() : Matrix3d.Identity;
                var pts = new Point3dCollection();
                foreach (double[] p in poly)
                {
                    Point3d w;
                    if (plane == "front") w = new Point3d(p[0], 0, p[1]);
                    else if (plane == "side") w = new Point3d(0, p[0], p[1]);
                    else w = new Point3d(p[0], p[1], 0);
                    Point3d n = w.TransformBy(toNative);
                    if (plane == "top") n = new Point3d(n.X, n.Y, 0);
                    pts.Add(n);
                }
                if (close && pts.Count > 0 && pts[0].DistanceTo(pts[pts.Count - 1]) > 1e-9) pts.Add(pts[0]);
                Vector3d normal = plane == "front" ? Vector3d.YAxis : plane == "side" ? Vector3d.XAxis : Vector3d.ZAxis;
                // 0.1.3: a crop drawn by the user in the FRONT view (T11) is stored
                // Rectangular with [x, 0, z] native vertices; our Polygonal front crop
                // with the same vertices hid everything. "auto" = rectangular for a
                // front/side outline of four points, polygonal on top (which works).
                string cropType = Str(r, "crop_type", "auto");
                if (cropType == "auto")
                    cropType = (plane != "top" && poly.Count == 4) ? "rectangular" : "polygonal";
                diag["crop_type"] = cropType;
                int before = pc.getCroppingCount();
                diag["crops_before"] = before;
                string used;
                if ((method == "auto" || method == "edit_existing") && replace && before > 0)
                {
                    while (pc.getCroppingCount() > 1) pc.removeLastCropping();
                    PointCloudCrop c = pc.getPointCloudCropping(0);
                    SetCrop(c, pts, planeMode, normal, inside, inverted, cropType, diag);
                    used = "edit_existing";
                }
                else if (method == "edit_existing")
                {
                    return Fail("invalid_args", "method edit_existing needs an existing crop and replace=true");
                }
                else
                {
                    if (replace) pc.clearCropping();
                    PointCloudCrop c = PointCloudCrop.Create(IntPtr.Zero);
                    SetCrop(c, pts, planeMode, normal, inside, inverted, cropType, diag);
                    pc.addCroppingBoundary(c);
                    used = "new";
                }
                diag["method_used"] = used;
                if (zmin != null && zmax != null && plane == "top")
                {
                    double z0 = Convert.ToDouble(zmin), z1 = Convert.ToDouble(zmax);
                    string zmode = Str(r, "zband", "front_crop");
                    if (zmode == "elevation")
                    {
                        diag["zband"] = ApplyHeightBand(pc, z0, z1, zspace);
                    }
                    else
                    {
                        // The band as a second crop seen from the FRONT, exactly the
                        // shape of the user's T11 crop: Rectangular, [x, 0, z] native,
                        // closed, no plane - over the outline's x range.
                        double xa = double.MaxValue, xb = double.MinValue;
                        foreach (double[] p in poly) { xa = Math.Min(xa, p[0]); xb = Math.Max(xb, p[0]); }
                        Matrix3d m = vertexSpace == "wcs" ? CloudTransform(pc).Inverse() : Matrix3d.Identity;
                        Point3d lo = new Point3d(xa, 0, z0).TransformBy(m), hi = new Point3d(xb, 0, z1).TransformBy(m);
                        var band = new Point3dCollection();
                        band.Add(new Point3d(lo.X, 0, lo.Z));
                        band.Add(new Point3d(hi.X, 0, lo.Z));
                        band.Add(new Point3d(hi.X, 0, hi.Z));
                        band.Add(new Point3d(lo.X, 0, hi.Z));
                        band.Add(new Point3d(lo.X, 0, lo.Z));
                        var bdiag = new Dictionary<string, object>();
                        PointCloudCrop bc = PointCloudCrop.Create(IntPtr.Zero);
                        SetCrop(bc, band, Str(r, "band_plane_mode", "none"), Vector3d.YAxis, true, false, "rectangular", bdiag);
                        pc.addCroppingBoundary(bc);
                        bdiag["mode"] = "front_crop";
                        diag["zband"] = bdiag;
                    }
                }
                pc.ShowCropped = true;
                diag["crops_after"] = pc.getCroppingCount();
                tr.Commit();
            }
            var d = Ok();
            d["diagnostics"] = diag;
            d["stored_crops"] = CropDetails(db, id);
            d["cloud"] = Describe(db, id, false);
            return d;
        }

        static void SetCrop(PointCloudCrop c, Point3dCollection pts, string planeMode, Vector3d normal,
                            bool inside, bool inverted, string cropType, Dictionary<string, object> diag)
        {
            c.CropType = cropType == "rectangular" ? PointCloudCropType.Rectangular : PointCloudCropType.Polygonal;
            // plane_mode: none (as AutoCAD's own crops read back), normal (plane through
            // the origin with the view's axis; "xy" is the 0.1.2 name), reverse (negated).
            if (planeMode == "normal" || planeMode == "xy") c.CropPlane = new Plane(Point3d.Origin, normal);
            else if (planeMode == "reverse") c.CropPlane = new Plane(Point3d.Origin, normal.Negate());
            diag["plane_mode"] = planeMode;
            c.Vertices = pts;
            c.Inside = inside;
            c.Inverted = inverted;
            diag["is_valid"] = Safe(delegate { return (object)c.IsValid(); });
            var sent = new List<object>();
            foreach (Point3d p in pts) sent.Add(P(p));
            diag["vertices_sent"] = sent;
        }

        // Height band through elevation colouring: points outside [zmin, zmax]
        // hidden. zband_space "wcs" converts WCS heights to the cloud's native
        // frame ((z - Location.Z) / Scale); "native" passes them as given. The
        // range is made fixed (ElevationApplyToFixedRange) - otherwise AutoCAD
        // follows the limit box and ignores it. AutoCAD's own read-back is returned.
        static Dictionary<string, object> ApplyHeightBand(PointCloudEx pc, double z0, double z1, string space)
        {
            var b = new Dictionary<string, object>();
            double n0 = z0, n1 = z1;
            if (space == "wcs" || space == "from_min")
            {
                double s = pc.Scale == 0 ? 1.0 : pc.Scale;
                n0 = (z0 - pc.Location.Z) / s;
                n1 = (z1 - pc.Location.Z) / s;
            }
            if (space == "from_min")
            {
                // elevation measured from the bottom of the cloud (native extents min z)
                double zlow = 0;
                try { zlow = pc.NativeExtents.MinPoint.Z; } catch (System.Exception) { }
                n0 -= zlow;
                n1 -= zlow;
            }
            // what elevation might be relative to: the limit box and every extent
            b["limit_box"] = Safe(delegate { var t = pc.LimitBoxBound; return (object)new[] { t.Item1, t.Item2 }; });
            b["geom_extents"] = Safe(delegate { return (object)E(pc.GeomExtents); });
            b["range_before"] = Safe(delegate { var t = pc.MinMaxElevation; return (object)new[] { t.Item1, t.Item2 }; });
            b["space"] = space;
            b["sent"] = new[] { n0, n1 };
            pc.Stylization = PointCloudStylizationType.HeightRamp;
            b["fixed_range"] = Safe(delegate { pc.ElevationApplyToFixedRange = true; return (object)pc.ElevationApplyToFixedRange; });
            pc.MinMaxElevation = Tuple.Create(Math.Min(n0, n1), Math.Max(n0, n1));
            pc.ElevationOutOfRangeBehavior = PointCloudDispOptionOutOfRange.HidePoints;
            b["stored"] = Safe(delegate { var t = pc.MinMaxElevation; return (object)new[] { t.Item1, t.Item2 }; });
            b["native_extents"] = Safe(delegate { return (object)E(pc.NativeExtents); });
            return b;
        }

        // 0.1.3 diagnostics: the cloud entity as AutoCAD itself writes it to DXF -
        // the managed PointCloudCrop.CropPlane getter returns garbage for crops the
        // user drew (T11), so the stored crop data are read from AutoCAD's own DXF
        // output instead: Wblock of the one entity into a new database, DxfOut to
        // a temp file, the entity's group codes returned. Read-only for the drawing.
        static Dictionary<string, object> Dump(Dictionary<string, object> r)
        {
            Document doc = AcApp.DocumentManager.MdiActiveDocument;
            Database db = doc.Database;
            ObjectId id;
            string why = Find(db, Str(r, "handle", null), out id);
            if (why != null) return Fail("not_found", why);
            string tmp = Path.Combine(Path.GetTempPath(), "mcp-pc-dump-" + Guid.NewGuid().ToString("N") + ".dxf");
            try
            {
                using (doc.LockDocument())
                {
                    var ids = new ObjectIdCollection();
                    ids.Add(id);
                    using (Database one = db.Wblock(ids, Point3d.Origin))
                    {
                        one.DxfOut(tmp, 16, DwgVersion.Current);
                    }
                }
                string[] lines = File.ReadAllLines(tmp);
                var codes = new List<object>();
                bool inEntity = false;
                for (int i = 0; i + 1 < lines.Length; i += 2)
                {
                    string code = lines[i].Trim(), value = lines[i + 1].Trim();
                    if (code == "0")
                    {
                        if (inEntity) break;
                        inEntity = value == "ACDBPOINTCLOUDEX";
                    }
                    if (inEntity && codes.Count < 3000) codes.Add(new[] { code, value });
                }
                var d = Ok();
                d["dxf_codes"] = codes;
                d["count"] = codes.Count;
                return d;
            }
            catch (System.Exception ex)
            {
                return Fail("api_error", "dump: " + ex.GetType().Name + ": " + ex.Message);
            }
            finally
            {
                try { if (File.Exists(tmp)) File.Delete(tmp); } catch (System.Exception) { }
            }
        }

        static Dictionary<string, object> CropClear(Dictionary<string, object> r)
        {
            Document doc = AcApp.DocumentManager.MdiActiveDocument;
            Database db = doc.Database;
            ObjectId id;
            string why = Find(db, Str(r, "handle", null), out id);
            if (why != null) return Fail("not_found", why);
            using (doc.LockDocument())
            using (Transaction tr = db.TransactionManager.StartTransaction())
            {
                PointCloudEx pc;
                string lockedWhy = OpenForWrite(db, tr, id, out pc);
                if (lockedWhy != null) return Fail("layer_locked", lockedWhy);
                pc.clearCropping();
                if (Bool(r, "reset_style", true))
                {
                    pc.ElevationOutOfRangeBehavior = PointCloudDispOptionOutOfRange.UseMinMaxColors;
                    pc.Stylization = PointCloudStylizationType.TrueColor;
                }
                tr.Commit();
            }
            var d = Ok();
            d["cloud"] = Describe(db, id, false);
            return d;
        }

        static Dictionary<string, object> Style(Dictionary<string, object> r)
        {
            Document doc = AcApp.DocumentManager.MdiActiveDocument;
            Database db = doc.Database;
            ObjectId id;
            string why = Find(db, Str(r, "handle", null), out id);
            if (why != null) return Fail("not_found", why);
            Dictionary<string, object> band = null;
            using (doc.LockDocument())
            using (Transaction tr = db.TransactionManager.StartTransaction())
            {
                PointCloudEx pc;
                string lockedWhy = OpenForWrite(db, tr, id, out pc);
                if (lockedWhy != null) return Fail("layer_locked", lockedWhy);
                string s = Str(r, "stylization", null);
                if (s != null)
                {
                    if (s == "truecolor") pc.Stylization = PointCloudStylizationType.TrueColor;
                    else if (s == "single") pc.Stylization = PointCloudStylizationType.SingleColor;
                    else if (s == "normal") pc.Stylization = PointCloudStylizationType.NormalRamp;
                    else if (s == "height") pc.Stylization = PointCloudStylizationType.HeightRamp;
                    else if (s == "intensity") pc.Stylization = PointCloudStylizationType.IntensityRamp;
                    else if (s == "classification") pc.Stylization = PointCloudStylizationType.ClassificationRamp;
                    else return Fail("invalid_args", "unknown stylization " + s);
                }
                string scheme = Str(r, "color_scheme", null);
                if (scheme != null) pc.CurrentColorScheme = scheme;
                var el = Nums(r, "elevation");
                if (el != null && el.Length == 2)
                    band = ApplyHeightBand(pc, el[0], el[1], Str(r, "zband_space", "wcs"));
                string oor = Str(r, "elevation_out_of_range", null);
                if (oor != null) pc.ElevationOutOfRangeBehavior = OutOfRange(oor);
                var it = Nums(r, "intensity");
                if (it != null && it.Length == 2) pc.MinMaxIntensity = Tuple.Create((int)it[0], (int)it[1]);
                string ioor = Str(r, "intensity_out_of_range", null);
                if (ioor != null) pc.IntensityOutOfRangeBehavior = OutOfRange(ioor);
                object show = Get(r, "show_cropped");
                if (show is bool) pc.ShowCropped = (bool)show;
                object scans = Get(r, "scans_visible");
                if (scans is bool) pc.setAllScansVisibility((bool)scans);
                object regions = Get(r, "regions_visible");
                if (regions is bool) pc.setAllRegionsVisibility((bool)regions, (bool)regions);
                tr.Commit();
            }
            var d = Ok();
            if (band != null) d["zband"] = band;
            d["cloud"] = Describe(db, id, false);
            return d;
        }

        // Section (0.1.2, EXPERIMENTAL). Live 2026-11-24 with 0.1.1: 16 polylines
        // 66-240 m long, far outside the building - AppendPolylineProfile placed
        // the extracted curves somewhere we do not understand. Now nothing is drawn
        // unless draw=true; the answer carries what Extract returned (curve count,
        // 2D extents of the raw curves, the result's transform) so the parameter
        // meaning can be settled live. space "wcs" passes origin/normal as given,
        // "native" converts the origin into the cloud's native frame first. When
        // drawn, each 2D curve is placed with the result's own transform.
        static Dictionary<string, object> Section(Dictionary<string, object> r)
        {
            Document doc = AcApp.DocumentManager.MdiActiveDocument;
            Database db = doc.Database;
            ObjectId id;
            string why = Find(db, Str(r, "handle", null), out id);
            if (why != null) return Fail("not_found", why);
            var state = Describe(db, id, false);
            object foundObj;
            if (!state.TryGetValue("file_found", out foundObj) || !(foundObj is bool) || !(bool)foundObj)
                return Fail("file_not_found", "the point cloud file is not on this computer; a section needs its points");
            Point3d origin = Pt(r, "origin", Point3d.Origin);
            Vector3d normal = Vec(r, "normal", Vector3d.ZAxis);
            Vector3d xdir = Vec(r, "xdir", Vector3d.XAxis);
            string layer = Str(r, "layer", "0");
            string space = Str(r, "space", "wcs");
            bool draw = Bool(r, "draw", false);
            var opt = new ExtractOption();
            if (Get(r, "fill_gap") != null) opt.FillGap = Num(r, "fill_gap", 0);
            if (Get(r, "snap_angle") != null) opt.SnapAngle = (uint)Num(r, "snap_angle", 0);
            if (Get(r, "min_segment") != null) opt.MiniumSegmentLength = Num(r, "min_segment", 0);
            if (Get(r, "lines_only") != null) opt.UseLineSegmentOnly = Bool(r, "lines_only", false);
            if (Get(r, "process_points") != null) opt.ProcessPoints = (uint)Num(r, "process_points", 0);
            var d = Ok();
            d["experimental"] = true;
            d["warning"] = "pointcloud_section is experimental: the meaning of PointCloudExtractor's arguments is not verified; check the diagnostics before trusting any line";
            var handles = new List<object>();
            int created = 0;
            using (doc.LockDocument())
            {
                PointCloudExtractResult res;
                using (Transaction tr = db.TransactionManager.StartTransaction())
                {
                    var pc = (PointCloudEx)tr.GetObject(id, OpenMode.ForRead);
                    Point3d o = origin;
                    if (space == "native") o = origin.TransformBy(CloudTransform(pc).Inverse());
                    d["origin_sent"] = P(o);
                    d["normal_sent"] = V(normal);
                    d["xdir_sent"] = V(xdir);
                    res = PointCloudExtractor.Extract(pc, normal, xdir, o, opt, new Progress());
                    if (draw) EnsureLayer(db, tr, layer);
                    tr.Commit();
                }
                if (res == null || res.Curves == null) return Fail("nothing_extracted", "the section returned no curves");
                d["curves"] = res.Curves.Length;
                d["transform"] = Safe(delegate { return (object)res.transform.ToArray(); });
                double x0 = double.MaxValue, y0 = double.MaxValue, x1 = double.MinValue, y1 = double.MinValue;
                double longest = 0;
                foreach (ProfileCurve2d c in res.Curves)
                {
                    try
                    {
                        Point2d a, b;
                        if (c.IsArc) { a = c.Arc.StartPoint; b = c.Arc.EndPoint; }
                        else { a = c.LineSeg.StartPoint; b = c.LineSeg.EndPoint; }
                        foreach (Point2d q in new[] { a, b })
                        {
                            x0 = Math.Min(x0, q.X); y0 = Math.Min(y0, q.Y);
                            x1 = Math.Max(x1, q.X); y1 = Math.Max(y1, q.Y);
                        }
                        longest = Math.Max(longest, a.GetDistanceTo(b));
                    }
                    catch (System.Exception) { }
                }
                if (res.Curves.Length > 0)
                {
                    d["raw_extents_2d"] = new[] { new[] { x0, y0 }, new[] { x1, y1 } };
                    d["raw_longest_segment"] = longest;
                }
                if (draw)
                {
                    using (Transaction tr = db.TransactionManager.StartTransaction())
                    {
                        var ms = (BlockTableRecord)tr.GetObject(SymbolUtilityServices.GetBlockModelSpaceId(db), OpenMode.ForWrite);
                        Matrix3d m = res.transform;
                        foreach (ProfileCurve2d c in res.Curves)
                        {
                            try
                            {
                                Entity e;
                                if (c.IsArc)
                                {
                                    CircularArc2d a = c.Arc;
                                    var p1 = new Point3d(a.StartPoint.X, a.StartPoint.Y, 0).TransformBy(m);
                                    var p2 = new Point3d(a.EndPoint.X, a.EndPoint.Y, 0).TransformBy(m);
                                    var mid = a.EvaluatePoint((a.StartAngle + a.EndAngle) / 2.0);
                                    var p3 = new Point3d(mid.X, mid.Y, 0).TransformBy(m);
                                    var arc3 = new CircularArc3d(p1, p3, p2);
                                    e = Curve.CreateFromGeCurve(arc3);
                                }
                                else
                                {
                                    var s = c.LineSeg;
                                    e = new Line(new Point3d(s.StartPoint.X, s.StartPoint.Y, 0).TransformBy(m),
                                                 new Point3d(s.EndPoint.X, s.EndPoint.Y, 0).TransformBy(m));
                                }
                                e.Layer = layer;
                                ms.AppendEntity(e);
                                tr.AddNewlyCreatedDBObject(e, true);
                                created++;
                                if (handles.Count < 200) handles.Add(e.Handle.ToString());
                            }
                            catch (System.Exception) { }
                        }
                        tr.Commit();
                    }
                }
            }
            d["created_count"] = created;
            d["handles"] = handles;
            d["layer"] = layer;
            return d;
        }

        class Progress : IPointCloudExtractionProgressCallback
        {
            bool cancelled;
            public void UpdateProgress(int p) { }
            public void UpdateCaption(string c) { }
            public bool Cancelled() { return cancelled; }
            public void Cancel() { cancelled = true; }
            public void UpdateRemainTime(double t) { }
            public void End() { }
        }

        // ── description ────────────────────────────────────────────────────
        //
        // AutoCAD 2021 crash (2026-11-24): pointcloud_info aborted AutoCAD with
        // "INTERNAL ERROR: !dbobji.cpp@8627: eNotOpenForWrite". Cause: the old code
        // called PointCloudEx.getPointCloudCropping(i) on an entity opened ForRead.
        // The ObjectARX/.NET reference says of getPointCloudCropping: "Returns a
        // cropping boundary by index, for editing. Requires that the
        // AcDbPointCloudEx be open for writing." (the const reader
        // getPointCloudCroppingConst is not exposed in the R24.0 managed API).
        // assertWriteEnabled is a fatal C++ abort - no try/catch can stop it -
        // so the open mode is what matters:
        //   * the basic description reads only const getters on a ForRead object;
        //   * crop details are read in their own transaction that opens the cloud
        //     ForWrite, changes nothing and is aborted - and only when asked
        //     (items) and the layer is not locked;
        //   * extents, definition counts, scans and regions are read only when the
        //     point cloud file is found on disk (an unloaded definition is not
        //     touched beyond its file names).

        static Dictionary<string, object> Describe(Database db, ObjectId id, bool items)
        {
            var d = new Dictionary<string, object>();
            d["handle"] = id.Handle.ToString();
            bool found = false;
            try
            {
                using (Transaction tr = db.TransactionManager.StartOpenCloseTransaction())
                {
                    var pc = (PointCloudEx)tr.GetObject(id, OpenMode.ForRead);
                    d["layer"] = Safe(delegate { return (object)pc.Layer; });
                    d["location"] = Safe(delegate { return (object)P(pc.Location); });
                    d["scale"] = Safe(delegate { return (object)pc.Scale; });
                    d["rotation_deg"] = Safe(delegate { return (object)(pc.Rotation * 180.0 / Math.PI); });
                    d["show_cropped"] = Safe(delegate { return (object)pc.ShowCropped; });
                    d["cropping_inverted"] = Safe(delegate { return (object)pc.CroppingInverted; });
                    d["crop_count"] = Safe(delegate { return (object)pc.getCroppingCount(); });
                    d["stylization"] = Safe(delegate { return (object)pc.Stylization.ToString(); });
                    d["elevation_out_of_range"] = Safe(delegate { return (object)pc.ElevationOutOfRangeBehavior.ToString(); });
                    string active = null, source = null;
                    try { active = pc.ActiveFileName; } catch (System.Exception) { }
                    PointCloudDefEx def = null;
                    try
                    {
                        def = (PointCloudDefEx)tr.GetObject(pc.PointCloudDefExId, OpenMode.ForRead);
                        if (string.IsNullOrEmpty(active)) active = def.ActiveFileName;
                        source = def.SourceFileName;
                    }
                    catch (System.Exception ex) { d["definition_error"] = ex.Message; }
                    string resolved = ResolveFile(db, active) ?? ResolveFile(db, source);
                    found = resolved != null;
                    d["path"] = active;
                    d["source_file"] = source;
                    d["file_found"] = found;
                    if (found) d["resolved_path"] = resolved;
                    if (found)
                    {
                        d["geom_extents"] = Safe(delegate { return (object)E(pc.GeomExtents); });
                        d["native_extents"] = Safe(delegate { return (object)E(pc.NativeExtents); });
                    }
                    if (items)
                    {
                        d["color_scheme"] = Safe(delegate { return (object)pc.CurrentColorScheme; });
                        d["elevation_range"] = Safe(delegate { var t = pc.MinMaxElevation; return (object)new[] { t.Item1, t.Item2 }; });
                        if (found && def != null)
                        {
                            var dd = new Dictionary<string, object>();
                            PointCloudDefEx df = def;
                            dd["file_type"] = Safe(delegate { return (object)df.FileType; });
                            dd["points"] = Safe(delegate { return (object)df.totalPointsCount; });
                            dd["scans"] = Safe(delegate { return (object)df.totalScansCount; });
                            dd["regions"] = Safe(delegate { return (object)df.totalRegionsCount; });
                            dd["extents"] = Safe(delegate { return (object)E(df.extents); });
                            dd["rcs_files"] = Safe(delegate { return (object)df.getAllRcsFilePaths(); });
                            d["definition"] = dd;
                            d["items"] = ItemRows(pc);
                        }
                        else
                        {
                            d["details_skipped"] = "file_not_found: the point cloud file is not on this computer; only the stored entity data is read";
                        }
                    }
                    tr.Commit();
                }
            }
            catch (System.Exception ex)
            {
                d["error"] = ex.GetType().Name + ": " + ex.Message;
            }
            if (items) d["crops"] = CropDetails(db, id);
            return d;
        }

        static List<object> ItemRows(PointCloudEx pc)
        {
            var rows = new List<object>();
            try
            {
                foreach (PointCloudItem it in pc.getPointCloudDataList())
                {
                    if (rows.Count >= 500) break;
                    var row = new Dictionary<string, object>();
                    row["name"] = it.Name;
                    row["guid"] = it.Guid;
                    row["id"] = it.ItemId;
                    row["category"] = it.CategoryId.ToString();
                    row["visible"] = it.Visibility;
                    if (!string.IsNullOrEmpty(it.Guid) && it.CategoryId == PointCloudItemType.Scan)
                    {
                        try
                        {
                            System.ValueType ov, ev;
                            if (pc.GetScanViewInfo(it.Guid, out ov, out ev))
                            {
                                if (ov is Point3d) row["scan_origin"] = P((Point3d)ov);
                                if (ev is Extents3d) row["scan_extents"] = E((Extents3d)ev);
                            }
                        }
                        catch (System.Exception ex) { row["scan_error"] = ex.Message; }
                    }
                    rows.Add(row);
                }
            }
            catch (System.Exception ex)
            {
                rows.Add(Fail("api_error", "getPointCloudDataList: " + ex.Message));
            }
            return rows;
        }

        // Crop boundaries can only be read from a cloud open for write (API rule,
        // see above). Opened ForWrite under a document lock, read, never changed,
        // the transaction aborted. A locked layer is not opened at all.
        static object CropDetails(Database db, ObjectId id)
        {
            try
            {
                string locked = LockedLayer(db, id);
                if (locked != null)
                    return Fail("layer_locked", "crop details need the cloud open for write; layer " + locked + " is locked");
                Document doc = AcApp.DocumentManager.MdiActiveDocument;
                var crops = new List<object>();
                using (doc.LockDocument())
                using (Transaction tr = db.TransactionManager.StartTransaction())
                {
                    var pc = (PointCloudEx)tr.GetObject(id, OpenMode.ForWrite);
                    int n = pc.getCroppingCount();
                    for (int i = 0; i < n; i++)
                    {
                        try
                        {
                            PointCloudCrop c = pc.getPointCloudCropping(i);
                            var cd = new Dictionary<string, object>();
                            cd["type"] = c.CropType.ToString();
                            cd["inside"] = c.Inside;
                            cd["inverted"] = c.Inverted;
                            Plane pl = c.CropPlane;
                            cd["plane_origin"] = P(pl.PointOnPlane);
                            cd["plane_normal"] = V(pl.Normal);
                            var vs = new List<object>();
                            foreach (Point3d v in c.Vertices) vs.Add(P(v));
                            cd["vertices"] = vs;
                            crops.Add(cd);
                        }
                        catch (System.Exception ex) { crops.Add(Fail("api_error", "getPointCloudCropping(" + i + "): " + ex.Message)); }
                    }
                    tr.Abort();  // read only: nothing was changed, nothing is kept
                }
                return crops;
            }
            catch (System.Exception ex)
            {
                return Fail("api_error", "crop details: " + ex.GetType().Name + ": " + ex.Message);
            }
        }

        // Layer name if the entity's layer is locked (ForWrite would throw eOnLockedLayer), else null.
        static string LockedLayer(Database db, ObjectId id)
        {
            using (Transaction tr = db.TransactionManager.StartOpenCloseTransaction())
            {
                var ent = (Entity)tr.GetObject(id, OpenMode.ForRead);
                var ltr = (LayerTableRecord)tr.GetObject(ent.LayerId, OpenMode.ForRead);
                string name = ltr.IsLocked ? ltr.Name : null;
                tr.Commit();
                return name;
            }
        }

        // A point cloud path as AutoCAD stores it may be relative (".\folder\x.rcp")
        // to the drawing's folder. Returns the existing full path, or null.
        static string ResolveFile(Database db, string path)
        {
            if (string.IsNullOrEmpty(path)) return null;
            try
            {
                if (Path.IsPathRooted(path) && File.Exists(path)) return path;
                string folder = null;
                try { folder = Path.GetDirectoryName(db.Filename); } catch (System.Exception) { }
                if (!string.IsNullOrEmpty(folder))
                {
                    string candidate = Path.GetFullPath(Path.Combine(folder, path));
                    if (File.Exists(candidate)) return candidate;
                }
            }
            catch (System.Exception) { }
            return null;
        }

        // Opens the cloud for write unless its layer is locked; null error = opened.
        static string OpenForWrite(Database db, Transaction tr, ObjectId id, out PointCloudEx pc)
        {
            pc = null;
            string locked = LockedLayer(db, id);
            if (locked != null) return "layer " + locked + " is locked; unlock it first";
            pc = (PointCloudEx)tr.GetObject(id, OpenMode.ForWrite);
            return null;
        }

        // ── helpers ────────────────────────────────────────────────────────

        static List<ObjectId> AllClouds(Database db)
        {
            var ids = new List<ObjectId>();
            RXClass cls = RXObject.GetClass(typeof(PointCloudEx));
            using (Transaction tr = db.TransactionManager.StartOpenCloseTransaction())
            {
                var ms = (BlockTableRecord)tr.GetObject(SymbolUtilityServices.GetBlockModelSpaceId(db), OpenMode.ForRead);
                foreach (ObjectId id in ms) if (id.ObjectClass.IsDerivedFrom(cls)) ids.Add(id);
                tr.Commit();
            }
            return ids;
        }

        static string Find(Database db, string handle, out ObjectId id)
        {
            id = ObjectId.Null;
            if (string.IsNullOrEmpty(handle))
            {
                var all = AllClouds(db);
                if (all.Count == 1) { id = all[0]; return null; }
                return all.Count == 0 ? "the drawing has no attached point cloud" : "several point clouds: give handle";
            }
            long value;
            try { value = Convert.ToInt64(handle, 16); }
            catch (System.Exception) { return "handle is not hexadecimal: " + handle; }
            if (!db.TryGetObjectId(new Handle(value), out id) || id.IsErased) return "no entity with handle " + handle;
            if (!id.ObjectClass.IsDerivedFrom(RXObject.GetClass(typeof(PointCloudEx))))
                return "handle " + handle + " is " + id.ObjectClass.DxfName + ", not a point cloud";
            return null;
        }

        static Matrix3d CloudTransform(PointCloudEx pc)
        {
            return Matrix3d.Displacement(pc.Location.GetAsVector())
                * Matrix3d.Rotation(pc.Rotation, Vector3d.ZAxis, Point3d.Origin)
                * Matrix3d.Scaling(pc.Scale, Point3d.Origin);
        }

        static void EnsureLayer(Database db, Transaction tr, string name)
        {
            var lt = (LayerTable)tr.GetObject(db.LayerTableId, OpenMode.ForRead);
            if (lt.Has(name)) return;
            lt.UpgradeOpen();
            var rec = new LayerTableRecord();
            rec.Name = name;
            lt.Add(rec);
            tr.AddNewlyCreatedDBObject(rec, true);
        }

        static PointCloudDispOptionOutOfRange OutOfRange(string s)
        {
            if (s == "hide") return PointCloudDispOptionOutOfRange.HidePoints;
            if (s == "rgb") return PointCloudDispOptionOutOfRange.UseRGBScanColors;
            return PointCloudDispOptionOutOfRange.UseMinMaxColors;
        }

        delegate object Getter();

        static object Safe(Getter g)
        {
            try { return g(); }
            catch (System.Exception ex) { return new Dictionary<string, object> { { "error", ex.Message } }; }
        }

        static Dictionary<string, object> Ok()
        {
            var d = new Dictionary<string, object>();
            d["ok"] = true;
            return d;
        }

        static Dictionary<string, object> Fail(string kind, string message)
        {
            var d = new Dictionary<string, object>();
            d["ok"] = false;
            d["kind"] = kind;
            d["error"] = message;
            return d;
        }

        static object Get(Dictionary<string, object> r, string k)
        {
            object v;
            return r.TryGetValue(k, out v) ? v : null;
        }

        static string Str(Dictionary<string, object> r, string k, string dflt)
        {
            object v = Get(r, k);
            return v == null ? dflt : Convert.ToString(v, System.Globalization.CultureInfo.InvariantCulture);
        }

        static double Num(Dictionary<string, object> r, string k, double dflt)
        {
            object v = Get(r, k);
            return v == null ? dflt : Convert.ToDouble(v, System.Globalization.CultureInfo.InvariantCulture);
        }

        static bool Bool(Dictionary<string, object> r, string k, bool dflt)
        {
            object v = Get(r, k);
            return v is bool ? (bool)v : dflt;
        }

        static double[] Nums(Dictionary<string, object> r, string k)
        {
            var a = Get(r, k) as ArrayList;
            if (a == null) { var o = Get(r, k) as object[]; if (o == null) return null; a = new ArrayList(o); }
            var res = new double[a.Count];
            for (int i = 0; i < a.Count; i++) res[i] = Convert.ToDouble(a[i], System.Globalization.CultureInfo.InvariantCulture);
            return res;
        }

        static List<double[]> List2(Dictionary<string, object> r, string k)
        {
            var res = new List<double[]>();
            var outer = Get(r, k) as IEnumerable;
            if (outer == null) return res;
            foreach (object p in outer)
            {
                var inner = p as IEnumerable;
                if (inner == null) continue;
                var xy = new List<double>();
                foreach (object c in inner) xy.Add(Convert.ToDouble(c, System.Globalization.CultureInfo.InvariantCulture));
                if (xy.Count >= 2) res.Add(new[] { xy[0], xy[1] });
            }
            return res;
        }

        static Point3d Pt(Dictionary<string, object> r, string k, Point3d dflt)
        {
            var n = Nums(r, k);
            if (n == null || n.Length < 2) return dflt;
            return new Point3d(n[0], n[1], n.Length > 2 ? n[2] : 0.0);
        }

        static Vector3d Vec(Dictionary<string, object> r, string k, Vector3d dflt)
        {
            var n = Nums(r, k);
            if (n == null || n.Length < 3) return dflt;
            return new Vector3d(n[0], n[1], n[2]);
        }

        static double[] P(Point3d p) { return new[] { p.X, p.Y, p.Z }; }
        static double[] V(Vector3d v) { return new[] { v.X, v.Y, v.Z }; }
        static object E(Extents3d e) { return new[] { P(e.MinPoint), P(e.MaxPoint) }; }
    }
}
