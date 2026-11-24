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
        public const string Version = "0.1.0";

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
            var js = new JavaScriptSerializer();
            js.MaxJsonLength = int.MaxValue;
            string outPath = req + ".out";
            Dictionary<string, object> res;
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
                else res = Fail("unknown_op", "unknown op " + op);
            }
            catch (System.Exception ex)
            {
                res = Fail("exception", ex.GetType().Name + ": " + ex.Message);
            }
            string tmp = outPath + ".part";
            File.WriteAllText(tmp, js.Serialize(res));
            if (File.Exists(outPath)) File.Delete(outPath);
            File.Move(tmp, outPath);
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
            bool items = Bool(r, "items", true);
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
            string vertexSpace = Str(r, "vertex_space", "wcs");
            object zmin = Get(r, "zmin");
            object zmax = Get(r, "zmax");
            string zmode = Str(r, "zband_mode", "elevation");
            int count;
            using (doc.LockDocument())
            using (Transaction tr = db.TransactionManager.StartTransaction())
            {
                var pc = (PointCloudEx)tr.GetObject(id, OpenMode.ForWrite);
                if (replace) pc.clearCropping();
                Matrix3d toNative = Matrix3d.Identity;
                if (vertexSpace == "native") toNative = CloudTransform(pc).Inverse();
                Plane basePlane;
                Vector3d normal;
                if (plane == "front") { normal = Vector3d.YAxis; }
                else if (plane == "side") { normal = Vector3d.XAxis; }
                else { normal = Vector3d.ZAxis; }
                basePlane = new Plane(Point3d.Origin, normal);
                var pts = new Point3dCollection();
                foreach (double[] p in poly)
                {
                    Point3d w;
                    if (plane == "front") w = new Point3d(p[0], 0, p[1]);
                    else if (plane == "side") w = new Point3d(0, p[0], p[1]);
                    else w = new Point3d(p[0], p[1], 0);
                    pts.Add(w.TransformBy(toNative));
                }
                AddCrop(pc, pts, basePlane, inside, inverted);
                if (zmin != null && zmax != null && plane == "top")
                {
                    double z0 = Convert.ToDouble(zmin), z1 = Convert.ToDouble(zmax);
                    if (zmode == "crop")
                    {
                        // A crop is a prism along its plane normal: the Z band is a
                        // second crop drawn in a vertical (XZ) plane over the polygon's X range.
                        double x0 = double.MaxValue, x1 = double.MinValue;
                        foreach (double[] p in poly) { x0 = Math.Min(x0, p[0]); x1 = Math.Max(x1, p[0]); }
                        var band = new Point3dCollection();
                        band.Add(new Point3d(x0, 0, z0).TransformBy(toNative));
                        band.Add(new Point3d(x1, 0, z0).TransformBy(toNative));
                        band.Add(new Point3d(x1, 0, z1).TransformBy(toNative));
                        band.Add(new Point3d(x0, 0, z1).TransformBy(toNative));
                        AddCrop(pc, band, new Plane(Point3d.Origin, Vector3d.YAxis), true, false);
                    }
                    else
                    {
                        pc.Stylization = PointCloudStylizationType.HeightRamp;
                        pc.MinMaxElevation = Tuple.Create(z0, z1);
                        pc.ElevationOutOfRangeBehavior = PointCloudDispOptionOutOfRange.HidePoints;
                    }
                }
                pc.ShowCropped = true;
                count = pc.getCroppingCount();
                tr.Commit();
            }
            var d = Ok();
            d["crops"] = count;
            d["cloud"] = Describe(db, id, false);
            return d;
        }

        static void AddCrop(PointCloudEx pc, Point3dCollection pts, Plane plane, bool inside, bool inverted)
        {
            PointCloudCrop c = PointCloudCrop.Create(IntPtr.Zero);
            c.CropType = PointCloudCropType.Polygonal;
            c.CropPlane = plane;
            c.Vertices = pts;
            c.Inside = inside;
            c.Inverted = inverted;
            pc.addCroppingBoundary(c);
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
                var pc = (PointCloudEx)tr.GetObject(id, OpenMode.ForWrite);
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
            using (doc.LockDocument())
            using (Transaction tr = db.TransactionManager.StartTransaction())
            {
                var pc = (PointCloudEx)tr.GetObject(id, OpenMode.ForWrite);
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
                if (el != null && el.Length == 2) pc.MinMaxElevation = Tuple.Create(el[0], el[1]);
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
            d["cloud"] = Describe(db, id, false);
            return d;
        }

        static Dictionary<string, object> Section(Dictionary<string, object> r)
        {
            Document doc = AcApp.DocumentManager.MdiActiveDocument;
            Database db = doc.Database;
            ObjectId id;
            string why = Find(db, Str(r, "handle", null), out id);
            if (why != null) return Fail("not_found", why);
            Point3d origin = Pt(r, "origin", Point3d.Origin);
            Vector3d normal = Vec(r, "normal", Vector3d.ZAxis);
            Vector3d xdir = Vec(r, "xdir", Vector3d.XAxis);
            string layer = Str(r, "layer", "0");
            var opt = new ExtractOption();
            if (Get(r, "fill_gap") != null) opt.FillGap = Num(r, "fill_gap", 0);
            if (Get(r, "snap_angle") != null) opt.SnapAngle = (uint)Num(r, "snap_angle", 0);
            if (Get(r, "min_segment") != null) opt.MiniumSegmentLength = Num(r, "min_segment", 0);
            if (Get(r, "lines_only") != null) opt.UseLineSegmentOnly = Bool(r, "lines_only", false);
            if (Get(r, "process_points") != null) opt.ProcessPoints = (uint)Num(r, "process_points", 0);
            int created = 0;
            var handles = new List<object>();
            int curves = 0;
            using (doc.LockDocument())
            {
                PointCloudExtractResult res;
                using (Transaction tr = db.TransactionManager.StartTransaction())
                {
                    var pc = (PointCloudEx)tr.GetObject(id, OpenMode.ForRead);
                    res = PointCloudExtractor.Extract(pc, normal, xdir, origin, opt, new Progress());
                    EnsureLayer(db, tr, layer);
                    tr.Commit();
                }
                if (res == null || res.Curves == null) return Fail("nothing_extracted", "the section returned no curves");
                curves = res.Curves.Length;
                ObjectId space = SymbolUtilityServices.GetBlockModelSpaceId(db);
                var color = Autodesk.AutoCAD.Colors.Color.FromColorIndex(Autodesk.AutoCAD.Colors.ColorMethod.ByLayer, 256);
                ObjectIdCollection ids;
                if (Bool(r, "as_polyline", true))
                    ids = PointCloudExtractor.AppendPolylineProfile(res, space, layer, color, Num(r, "polyline_param", 0.0));
                else
                    ids = PointCloudExtractor.AppendLineProfile(res, space, layer, color);
                if (ids != null)
                {
                    foreach (ObjectId e in ids) { created++; if (handles.Count < 200) handles.Add(e.Handle.ToString()); }
                }
            }
            var d = Ok();
            d["curves"] = curves;
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

        static Dictionary<string, object> Describe(Database db, ObjectId id, bool items)
        {
            var d = new Dictionary<string, object>();
            using (Transaction tr = db.TransactionManager.StartOpenCloseTransaction())
            {
                var pc = (PointCloudEx)tr.GetObject(id, OpenMode.ForRead);
                d["handle"] = id.Handle.ToString();
                d["layer"] = pc.Layer;
                d["path"] = Safe(delegate { return (object)pc.ActiveFileName; });
                d["location"] = P(pc.Location);
                d["scale"] = pc.Scale;
                d["rotation_deg"] = pc.Rotation * 180.0 / Math.PI;
                d["geom_extents"] = Safe(delegate { return (object)E(pc.GeomExtents); });
                d["native_extents"] = Safe(delegate { return (object)E(pc.NativeExtents); });
                d["show_cropped"] = pc.ShowCropped;
                d["cropping_inverted"] = pc.CroppingInverted;
                d["stylization"] = pc.Stylization.ToString();
                d["color_scheme"] = Safe(delegate { return (object)pc.CurrentColorScheme; });
                d["elevation_range"] = Safe(delegate { var t = pc.MinMaxElevation; return (object)new[] { t.Item1, t.Item2 }; });
                d["elevation_out_of_range"] = pc.ElevationOutOfRangeBehavior.ToString();
                int n = pc.getCroppingCount();
                var crops = new List<object>();
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
                    catch (System.Exception ex) { crops.Add(new Dictionary<string, object> { { "error", ex.Message } }); }
                }
                d["crops"] = crops;
                try
                {
                    var def = (PointCloudDefEx)tr.GetObject(pc.PointCloudDefExId, OpenMode.ForRead);
                    var dd = new Dictionary<string, object>();
                    dd["source_file"] = def.SourceFileName;
                    dd["active_file"] = def.ActiveFileName;
                    dd["file_type"] = def.FileType;
                    dd["points"] = def.totalPointsCount;
                    dd["scans"] = def.totalScansCount;
                    dd["regions"] = def.totalRegionsCount;
                    dd["extents"] = Safe(delegate { return (object)E(def.extents); });
                    dd["rcs_files"] = Safe(delegate { return (object)def.getAllRcsFilePaths(); });
                    d["definition"] = dd;
                }
                catch (System.Exception ex) { d["definition"] = new Dictionary<string, object> { { "error", ex.Message } }; }
                if (items)
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
                                catch (System.Exception) { }
                            }
                            rows.Add(row);
                        }
                    }
                    catch (System.Exception ex) { rows.Add(new Dictionary<string, object> { { "error", ex.Message } }); }
                    d["items"] = rows;
                }
                tr.Commit();
            }
            return d;
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
