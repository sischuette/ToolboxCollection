# -*- coding: utf-8 -*-
"""
Tin3DToLandXML.pyt
Version: 1.0

ArcGIS Pro Python toolbox for exporting one or more Esri TIN
layers/datasets to LandXML 1.2 surface XML.

Target platform:
    ArcGIS Pro 3.5 or newer
    Uses only ArcGIS Pro standard Python modules and arcpy.

Implementation notes:	
	* ArcGIS Pro includes an Esri-supported LandXML To TIN importer. This is an additional exporter.
    * Input is a multi-value TIN Layer parameter. This allows TINs to be dragged
      from Contents and also allows catalog TIN datasets to be browsed/entered.
    * ArcPy does not provide a documented simple Python iterator over TIN nodes
      and triangle faces. This toolbox therefore uses supported 3D Analyst tools:
          - arcpy.ddd.TinNode
          - arcpy.ddd.TinTriangle
      and serializes the resulting 3D point/polygon features to LandXML.
    * LandXML Surface Definition writes <Pnts>/<P> points and <Faces>/<F>
      triangle faces. LandXML convention stores point coordinates as
      north/east/elev, equivalent to Y/X/Z for projected GIS data.
	* Some features are still experimental and may not fully work.
      Tested with files from http://www.landxml.org/webapps/LandXMLSamples.aspx.

Parameter help summary:
    Input TINs:
        One or more Esri TIN layers or TIN datasets. Drag TIN layers from the
        Contents pane or browse to TIN datasets. Inputs can be exported to
        separate LandXML files or combined into one LandXML file.
    Output folder:
        Folder where generated .xml/.landxml files are written. Defaults to the
        current ArcGIS Pro project home folder when available.
    Write all input TINs to one LandXML file:
        Checkbox. When enabled, all input TINs are written as separate
        <Surface> elements in one LandXML file. When disabled, each TIN is
        written to a separate LandXML file.
    Output file name template:
        Optional template used for output file names. Use {tin} for the source
        TIN name and {index} for a 1-based sequence number. Example:
        {tin}.xml or export_{index}_{tin}.landxml. In combined mode, {tin}
        resolves to Combined_TINs and {index} resolves to 1.
    Surface name template:
        Optional template used as the LandXML <Surface name>. Use {tin} and
        {index}. Defaults to the source TIN name.
    LandXML point coordinate order:
        LandXML convention is Northing Easting Elevation (Y X Z). Select X Y Z
        only if your target system expects non-conventional coordinate order.
    LandXML linear unit:
        Unit metadata written to LandXML <Units>. Coordinates are not converted;
        choose the unit matching the input TIN coordinates.
    Z unit metadata:
        Informational metadata recorded in the Application description. The
        LandXML 1.2 Metric unit block has one linearUnit attribute; coordinates
        are not converted.
    Coordinate system metadata mode:
        Use input spatial reference metadata, custom name/EPSG metadata, or omit
        coordinate system metadata. Coordinates are not reprojected.
    Decimal places:
        Number of decimal places used when matching triangle vertices to nodes
        and formatting coordinates. Six is normally safe for projected data.
    Include surface extents:
        Writes Definition/Extents/Min and Max values.
    Validate triangle vertices:
        When enabled, triangles whose vertices cannot be matched to exported TIN
        nodes are skipped with warnings. When disabled, such mismatches stop the
        export.
    Overwrite existing LandXML files:
        Allows the tool to replace existing output files.
    Save logs:
        Checkbox. When enabled, writes a .log file next to each generated
        LandXML file. The log file uses the same base name as the LandXML
        output, for example MyTin.xml and MyTin.log. No log folder/path is
        requested from the user.
"""

from __future__ import annotations

import datetime as _datetime
import logging as _logging
import math as _math
import os as _os
import re as _re
import tempfile as _tempfile
import traceback as _traceback
import xml.etree.ElementTree as _ET

import arcpy


LANDXML_NS = "http://www.landxml.org/schema/LandXML-1.2"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
_ET.register_namespace("", LANDXML_NS)
_ET.register_namespace("xsi", XSI_NS)


class Toolbox(object):
    def __init__(self):
        self.label = "TIN 3D to LandXML"
        self.alias = "tin3d_landxml"
        self.tools = [Tin3DToLandXML]


class Tin3DToLandXML(object):
    def __init__(self):
        self.label = "Export TINs to LandXML"
        self.description = (
            "Exports one or more Esri TIN layers or TIN datasets to LandXML 1.2 "
            "surface XML using Pnts and Faces. Supports drag-and-drop TIN layers "
            "from Contents and multi-value input."
        )
        self.canRunInBackground = False

    def getParameterInfo(self):
        in_tins = arcpy.Parameter(
            displayName="Input TINs",
            name="in_tins",
            datatype=["GPTinLayer", "DETin"],
            parameterType="Required",
            direction="Input",
            multiValue=True,
        )
        in_tins.category = "Input"

        out_folder = arcpy.Parameter(
            displayName="Output folder",
            name="out_folder",
            datatype="DEFolder",
            parameterType="Required",
            direction="Output",
        )
        out_folder.category = "Output"
        default_home = self._current_project_home_folder()
        if default_home:
            out_folder.value = default_home

        combine_outputs = arcpy.Parameter(
            displayName="Write all input TINs to one LandXML file",
            name="combine_outputs",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input",
        )
        combine_outputs.value = False
        combine_outputs.category = "Output"

        out_name_template = arcpy.Parameter(
            displayName="Output file name template",
            name="out_name_template",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
        )
        out_name_template.value = "{tin}.xml"
        out_name_template.category = "Output"

        surface_name_template = arcpy.Parameter(
            displayName="Surface name template",
            name="surface_name_template",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
        )
        surface_name_template.value = "{tin}"
        surface_name_template.category = "LandXML Surface"

        coord_order = arcpy.Parameter(
            displayName="LandXML point coordinate order",
            name="coordinate_order",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
        )
        coord_order.filter.type = "ValueList"
        coord_order.filter.list = [
            "Northing Easting Elevation (Y X Z) - LandXML conventional",
            "Easting Northing Elevation (X Y Z)",
        ]
        coord_order.value = coord_order.filter.list[0]
        coord_order.category = "LandXML Surface"

        linear_unit = arcpy.Parameter(
            displayName="LandXML linear unit metadata",
            name="linear_unit",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
        )
        linear_unit.filter.type = "ValueList"
        linear_unit.filter.list = [
            "meter",
            "internationalFoot",
            "surveyFoot",
            "millimeter",
            "centimeter",
            "kilometer",
        ]
        linear_unit.value = "meter"
        linear_unit.category = "Units and Coordinates"

        z_unit = arcpy.Parameter(
            displayName="Z unit metadata",
            name="z_unit",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
        )
        z_unit.filter.type = "ValueList"
        z_unit.filter.list = [
            "same as linear unit",
            "meter",
            "internationalFoot",
            "surveyFoot",
            "millimeter",
            "centimeter",
            "kilometer",
        ]
        z_unit.value = "same as linear unit"
        z_unit.category = "Units and Coordinates"

        coord_sys_mode = arcpy.Parameter(
            displayName="Coordinate system metadata mode",
            name="coord_sys_mode",
            datatype="GPString",
            parameterType="Required",
            direction="Input",
        )
        coord_sys_mode.filter.type = "ValueList"
        coord_sys_mode.filter.list = [
            "Use input TIN spatial reference",
            "Use custom metadata below",
            "Omit coordinate system metadata",
        ]
        coord_sys_mode.value = coord_sys_mode.filter.list[0]
        coord_sys_mode.category = "Units and Coordinates"

        custom_coord_sys_name = arcpy.Parameter(
            displayName="Custom coordinate system name",
            name="custom_coord_sys_name",
            datatype="GPString",
            parameterType="Optional",
            direction="Input",
        )
        custom_coord_sys_name.category = "Units and Coordinates"

        custom_epsg = arcpy.Parameter(
            displayName="Custom EPSG code",
            name="custom_epsg",
            datatype="GPLong",
            parameterType="Optional",
            direction="Input",
        )
        custom_epsg.category = "Units and Coordinates"

        precision = arcpy.Parameter(
            displayName="Coordinate decimal places",
            name="decimal_places",
            datatype="GPLong",
            parameterType="Required",
            direction="Input",
        )
        precision.value = 6
        precision.category = "Processing"

        include_extents = arcpy.Parameter(
            displayName="Include surface extents",
            name="include_extents",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input",
        )
        include_extents.value = True
        include_extents.category = "Processing"

        validate_triangles = arcpy.Parameter(
            displayName="Validate triangle vertices against exported TIN nodes",
            name="validate_triangles",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input",
        )
        validate_triangles.value = True
        validate_triangles.category = "Processing"

        overwrite = arcpy.Parameter(
            displayName="Overwrite existing LandXML files",
            name="overwrite_existing",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input",
        )
        overwrite.value = False
        overwrite.category = "Output"

        save_logs = arcpy.Parameter(
            displayName="Save logs",
            name="save_logs",
            datatype="GPBoolean",
            parameterType="Optional",
            direction="Input",
        )
        save_logs.value = False
        save_logs.category = "Diagnostics"

        return [
            in_tins,
            out_folder,
            combine_outputs,
            out_name_template,
            surface_name_template,
            coord_order,
            linear_unit,
            z_unit,
            coord_sys_mode,
            custom_coord_sys_name,
            custom_epsg,
            precision,
            include_extents,
            validate_triangles,
            overwrite,
            save_logs,
        ]

    def isLicensed(self):
        return arcpy.CheckExtension("3D") == "Available"

    def updateParameters(self, parameters):
        if not parameters[1].altered and not parameters[1].valueAsText:
            default_home = self._current_project_home_folder()
            if default_home:
                parameters[1].value = default_home

        mode = parameters[8].valueAsText
        use_custom = mode == "Use custom metadata below"
        parameters[9].enabled = use_custom
        parameters[10].enabled = use_custom
        return

    def updateMessages(self, parameters):
        if parameters[3].valueAsText:
            template = parameters[3].valueAsText.strip()
            if "{tin}" not in template and "{index}" not in template:
                parameters[3].setWarningMessage(
                    "For multiple separate TIN exports, include {tin} or {index} to avoid duplicate output names."
                )
            if not template.lower().endswith((".xml", ".landxml")):
                parameters[3].setWarningMessage(
                    "LandXML files are usually written with .xml or .landxml extension."
                )
        if parameters[11].value is not None:
            places = int(parameters[11].value)
            if places < 0 or places > 12:
                parameters[11].setErrorMessage("Decimal places must be between 0 and 12.")
        return

    def execute(self, parameters, messages):
        in_tin_values = self._parse_multivalue(parameters[0].valueAsText)
        out_folder = parameters[1].valueAsText or self._current_project_home_folder()
        combine_outputs = self._as_bool(parameters[2].value)
        out_name_template = parameters[3].valueAsText or "{tin}.xml"
        surface_name_template = parameters[4].valueAsText or "{tin}"
        coord_order = parameters[5].valueAsText
        linear_unit = parameters[6].valueAsText
        z_unit = parameters[7].valueAsText or "same as linear unit"
        coord_sys_mode = parameters[8].valueAsText
        custom_coord_sys_name = parameters[9].valueAsText
        custom_epsg = parameters[10].value
        decimal_places = int(parameters[11].value)
        include_extents = self._as_bool(parameters[12].value)
        validate_triangles = self._as_bool(parameters[13].value)
        overwrite = self._as_bool(parameters[14].value)
        save_logs = self._as_bool(parameters[15].value)

        logger = self._configure_logger(None)
        arcpy.SetProgressor("default", "Preparing TIN to LandXML export...")

        try:
            arcpy.CheckOutExtension("3D")
            if not in_tin_values:
                raise arcpy.ExecuteError("At least one input TIN is required.")
            if not out_folder:
                raise arcpy.ExecuteError("Output folder is required.")
            if not _os.path.isdir(out_folder):
                _os.makedirs(out_folder)

            logger.info("Input TIN count: %s", len(in_tin_values))
            logger.info("Output folder: %s", out_folder)

            exported = []
            if combine_outputs:
                out_name = self._format_template(out_name_template, "Combined_TINs", 1)
                if not out_name.lower().endswith((".xml", ".landxml")):
                    out_name += ".xml"
                out_xml = _os.path.join(out_folder, out_name)
                if _os.path.exists(out_xml) and not overwrite:
                    raise arcpy.ExecuteError(
                        "Output file already exists and overwrite is disabled: {0}".format(out_xml)
                    )

                combined_logger = self._configure_logger(self._log_path_for_output(out_xml) if save_logs else None)
                if save_logs:
                    combined_logger.info("Log file: %s", self._log_path_for_output(out_xml))

                surfaces = []
                for index, tin_value in enumerate(in_tin_values, start=1):
                    tin_path = self._resolve_tin_path(tin_value, combined_logger)
                    tin_name = self._safe_name(self._tin_name(tin_path, tin_value))
                    surface_name = self._format_template(surface_name_template, tin_name, index)
                    arcpy.AddMessage("Collecting {0} for combined LandXML -> {1}".format(tin_value, out_xml))
                    combined_logger.info("Resolved input %s to %s", tin_value, tin_path)
                    surfaces.append(
                        self._collect_surface(
                            in_tin=tin_path,
                            surface_name=surface_name,
                            coord_sys_mode=coord_sys_mode,
                            custom_coord_sys_name=custom_coord_sys_name,
                            custom_epsg=custom_epsg,
                            decimal_places=decimal_places,
                            validate_triangles=validate_triangles,
                            logger=combined_logger,
                        )
                    )

                arcpy.SetProgressorLabel("Writing combined LandXML document...")
                self._write_landxml(
                    out_xml=out_xml,
                    surfaces=surfaces,
                    coord_order=coord_order,
                    linear_unit=linear_unit,
                    z_unit=z_unit,
                    decimal_places=decimal_places,
                    include_extents=include_extents,
                    logger=combined_logger,
                )
                exported.append(out_xml)
                arcpy.AddMessage("Exported {0} TIN surface(s) to {1}".format(len(surfaces), out_xml))
            else:
                for index, tin_value in enumerate(in_tin_values, start=1):
                    tin_path = self._resolve_tin_path(tin_value, logger)
                    tin_name = self._safe_name(self._tin_name(tin_path, tin_value))
                    out_name = self._format_template(out_name_template, tin_name, index)
                    if not out_name.lower().endswith((".xml", ".landxml")):
                        out_name += ".xml"
                    out_xml = _os.path.join(out_folder, out_name)
                    surface_name = self._format_template(surface_name_template, tin_name, index)

                    if _os.path.exists(out_xml) and not overwrite:
                        raise arcpy.ExecuteError(
                            "Output file already exists and overwrite is disabled: {0}".format(out_xml)
                        )

                    per_file_logger = self._configure_logger(self._log_path_for_output(out_xml) if save_logs else None)
                    arcpy.AddMessage("Exporting {0} -> {1}".format(tin_value, out_xml))
                    per_file_logger.info("Resolved input %s to %s", tin_value, tin_path)
                    if save_logs:
                        per_file_logger.info("Log file: %s", self._log_path_for_output(out_xml))
                    self._export_one(
                        in_tin=tin_path,
                        out_xml=out_xml,
                        surface_name=surface_name,
                        coord_order=coord_order,
                        linear_unit=linear_unit,
                        z_unit=z_unit,
                        coord_sys_mode=coord_sys_mode,
                        custom_coord_sys_name=custom_coord_sys_name,
                        custom_epsg=custom_epsg,
                        decimal_places=decimal_places,
                        include_extents=include_extents,
                        validate_triangles=validate_triangles,
                        logger=per_file_logger,
                    )
                    exported.append(out_xml)

            logger.info("Finished LandXML export successfully. Files: %s", "; ".join(exported))
            arcpy.AddMessage("Exported {0} LandXML file(s).".format(len(exported)))

        except Exception as ex:
            logger.error("Export failed: %s", ex)
            logger.error(_traceback.format_exc())
            arcpy.AddError(str(ex))
            raise
        finally:
            try:
                arcpy.CheckInExtension("3D")
            except Exception:
                pass
            arcpy.ResetProgressor()

    def _export_one(
        self,
        in_tin,
        out_xml,
        surface_name,
        coord_order,
        linear_unit,
        z_unit,
        coord_sys_mode,
        custom_coord_sys_name,
        custom_epsg,
        decimal_places,
        include_extents,
        validate_triangles,
        logger,
    ):
        surface = self._collect_surface(
            in_tin=in_tin,
            surface_name=surface_name,
            coord_sys_mode=coord_sys_mode,
            custom_coord_sys_name=custom_coord_sys_name,
            custom_epsg=custom_epsg,
            decimal_places=decimal_places,
            validate_triangles=validate_triangles,
            logger=logger,
        )

        arcpy.SetProgressorLabel("Writing LandXML document...")
        self._write_landxml(
            out_xml=out_xml,
            surfaces=[surface],
            coord_order=coord_order,
            linear_unit=linear_unit,
            z_unit=z_unit,
            decimal_places=decimal_places,
            include_extents=include_extents,
            logger=logger,
        )

        arcpy.AddMessage(
            "Exported {0} nodes and {1} faces to {2}".format(
                len(surface["points"]), len(surface["faces"]), out_xml
            )
        )

    def _collect_surface(
        self,
        in_tin,
        surface_name,
        coord_sys_mode,
        custom_coord_sys_name,
        custom_epsg,
        decimal_places,
        validate_triangles,
        logger,
    ):
        arcpy.SetProgressorLabel("Validating input TIN...")
        if not arcpy.Exists(in_tin):
            raise arcpy.ExecuteError("Input TIN does not exist or cannot be resolved: {0}".format(in_tin))

        desc = arcpy.Describe(in_tin)
        data_type = getattr(desc, "dataType", "")
        if data_type not in ("Tin", "TinLayer"):
            # Some ArcGIS versions expose catalog TINs as 'Tin' and layer inputs as 'TinLayer'.
            raise arcpy.ExecuteError("The value is not a TIN or TIN layer: {0} (dataType={1})".format(in_tin, data_type))

        scratch_folder = _tempfile.mkdtemp(prefix="tin_landxml_")
        scratch_gdb = _os.path.join(scratch_folder, "scratch.gdb")
        arcpy.management.CreateFileGDB(scratch_folder, "scratch.gdb")
        logger.info("Scratch workspace: %s", scratch_gdb)

        node_fc = _os.path.join(scratch_gdb, "tin_nodes")
        tri_fc = _os.path.join(scratch_gdb, "tin_triangles")

        arcpy.SetProgressorLabel("Extracting TIN nodes...")
        arcpy.ddd.TinNode(in_tin, node_fc)
        node_count = int(arcpy.management.GetCount(node_fc)[0])
        logger.info("Exported %s TIN nodes.", node_count)
        if node_count == 0:
            raise arcpy.ExecuteError("The TIN did not export any nodes.")

        arcpy.SetProgressorLabel("Extracting TIN triangle faces...")
        # Unit parameter affects slope/aspect attributes only. Geometry is preserved.
        arcpy.ddd.TinTriangle(in_tin, tri_fc, "PERCENT")
        tri_count = int(arcpy.management.GetCount(tri_fc)[0])
        logger.info("Exported %s TIN triangle polygons.", tri_count)
        if tri_count == 0:
            raise arcpy.ExecuteError("The TIN did not export any triangle faces.")

        arcpy.SetProgressorLabel("Reading nodes and building vertex index...")
        points, key_to_id, bounds = self._read_nodes(node_fc, decimal_places, logger)

        arcpy.SetProgressor("step", "Reading triangle faces...", 0, tri_count, 1)
        faces, skipped = self._read_faces(
            tri_fc,
            key_to_id,
            decimal_places,
            validate_triangles,
            logger,
        )
        arcpy.ResetProgressor()

        if not faces:
            raise arcpy.ExecuteError("No valid triangular faces could be written.")
        if skipped:
            arcpy.AddWarning("Skipped {0} triangle(s) that could not be mapped to nodes.".format(skipped))
            logger.warning("Skipped %s triangle(s).", skipped)

        spatial_ref = getattr(arcpy.Describe(in_tin), "spatialReference", None)
        coord_meta = self._coordinate_system_metadata(
            spatial_ref,
            coord_sys_mode,
            custom_coord_sys_name,
            custom_epsg,
        )

        return {
            "name": surface_name,
            "points": points,
            "faces": faces,
            "bounds": bounds,
            "coord_meta": coord_meta,
        }

    @staticmethod
    def _current_project_home_folder():
        try:
            project = arcpy.mp.ArcGISProject("CURRENT")
            home = getattr(project, "homeFolder", None)
            if home:
                return home
        except Exception:
            pass
        return None

    @staticmethod
    def _as_bool(value):
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        text = str(value).strip().lower()
        return text in ("true", "1", "yes", "y")

    @staticmethod
    def _parse_multivalue(value_text):
        if not value_text:
            return []
        values = []
        current = []
        in_quote = False
        quote_char = None
        for ch in value_text:
            if ch in ('"', "'"):
                if in_quote and ch == quote_char:
                    in_quote = False
                    quote_char = None
                elif not in_quote:
                    in_quote = True
                    quote_char = ch
                else:
                    current.append(ch)
            elif ch == ";" and not in_quote:
                item = "".join(current).strip().strip('"').strip("'")
                if item:
                    values.append(item)
                current = []
            else:
                current.append(ch)
        item = "".join(current).strip().strip('"').strip("'")
        if item:
            values.append(item)
        return values

    @staticmethod
    def _resolve_tin_path(value, logger):
        # Dragging from Contents may pass a layer name. Browsing passes a dataset path.
        # ArcPy geoprocessing tools can often use either directly. If Describe exposes
        # a catalogPath or dataSource, prefer that so output naming and validation are stable.
        try:
            desc = arcpy.Describe(value)
            for attr in ("catalogPath", "dataSource"):
                try:
                    candidate = getattr(desc, attr, None)
                    if candidate and arcpy.Exists(candidate):
                        return candidate
                except Exception:
                    pass
        except Exception as ex:
            logger.warning("Could not describe input as layer; using raw value. Details: %s", ex)
        return value

    @staticmethod
    def _tin_name(tin_path, original_value):
        try:
            desc = arcpy.Describe(tin_path)
            name = getattr(desc, "name", None) or getattr(desc, "baseName", None)
            if name:
                return name
        except Exception:
            pass
        base = _os.path.basename(_os.path.normpath(tin_path or original_value))
        return base or "TIN_Surface"

    @staticmethod
    def _safe_name(name):
        text = _re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name).strip())
        return text.strip("._") or "TIN_Surface"

    @staticmethod
    def _format_template(template, tin_name, index):
        try:
            return template.format(tin=tin_name, index=index)
        except Exception:
            return "{0}_{1}.xml".format(index, tin_name)

    @staticmethod
    def _log_path_for_output(out_xml):
        base, _ext = _os.path.splitext(out_xml)
        return base + ".log"

    @staticmethod
    def _configure_logger(log_file):
        logger = _logging.getLogger("Tin3DToLandXML")
        logger.setLevel(_logging.INFO)
        logger.handlers = []

        formatter = _logging.Formatter("%(asctime)s %(levelname)s %(message)s")

        class ArcPyHandler(_logging.Handler):
            def emit(self, record):
                msg = self.format(record)
                if record.levelno >= _logging.ERROR:
                    arcpy.AddError(msg)
                elif record.levelno >= _logging.WARNING:
                    arcpy.AddWarning(msg)
                else:
                    arcpy.AddMessage(msg)

        gp_handler = ArcPyHandler()
        gp_handler.setFormatter(formatter)
        logger.addHandler(gp_handler)

        if log_file:
            file_handler = _logging.FileHandler(log_file, mode="w", encoding="utf-8")
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)

        return logger

    @staticmethod
    def _coord_key(x, y, z, decimal_places):
        return (
            round(float(x), decimal_places),
            round(float(y), decimal_places),
            round(float(z), decimal_places),
        )

    def _read_nodes(self, node_fc, decimal_places, logger):
        points = []
        key_to_id = {}
        min_x = min_y = min_z = float("inf")
        max_x = max_y = max_z = float("-inf")

        next_id = 1
        with arcpy.da.SearchCursor(node_fc, ["SHAPE@XYZ"]) as cursor:
            for (xyz,) in cursor:
                if xyz is None or len(xyz) < 3:
                    continue
                x, y, z = xyz
                if z is None or _math.isnan(float(z)):
                    raise arcpy.ExecuteError("TIN node without Z value encountered.")

                key = self._coord_key(x, y, z, decimal_places)
                if key in key_to_id:
                    logger.warning("Duplicate node coordinate found and ignored: %s", key)
                    continue

                pid = next_id
                next_id += 1
                key_to_id[key] = pid
                points.append((pid, float(x), float(y), float(z)))
                min_x, max_x = min(min_x, x), max(max_x, x)
                min_y, max_y = min(min_y, y), max(max_y, y)
                min_z, max_z = min(min_z, z), max(max_z, z)

        if not points:
            raise arcpy.ExecuteError("No readable 3D nodes were found.")

        bounds = {
            "min_x": min_x,
            "min_y": min_y,
            "min_z": min_z,
            "max_x": max_x,
            "max_y": max_y,
            "max_z": max_z,
        }
        return points, key_to_id, bounds

    def _read_faces(self, tri_fc, key_to_id, decimal_places, validate_triangles, logger):
        faces = []
        skipped = 0

        with arcpy.da.SearchCursor(tri_fc, ["OID@", "SHAPE@"], explode_to_points=False) as cursor:
            for oid, geom in cursor:
                arcpy.SetProgressorPosition()
                vertices = self._triangle_vertices(geom)
                if len(vertices) != 3:
                    skipped += 1
                    logger.warning("Triangle OID %s did not resolve to exactly 3 unique vertices.", oid)
                    continue

                face_ids = []
                missing = False
                for x, y, z in vertices:
                    if z is None or _math.isnan(float(z)):
                        raise arcpy.ExecuteError("Triangle OID {0} has a vertex without Z value.".format(oid))
                    key = self._coord_key(x, y, z, decimal_places)
                    pid = key_to_id.get(key)
                    if pid is None:
                        missing = True
                        if validate_triangles:
                            logger.warning("Triangle OID %s vertex not found in TIN nodes: %s", oid, key)
                        break
                    face_ids.append(pid)

                if missing:
                    skipped += 1
                    if validate_triangles:
                        continue
                    raise arcpy.ExecuteError(
                        "Triangle vertices could not be matched to TIN nodes. Increase decimal places "
                        "only if coordinates are being over-rounded, or inspect the TIN geometry."
                    )

                faces.append(tuple(face_ids))

        return faces, skipped

    @staticmethod
    def _triangle_vertices(geom):
        unique = []
        seen = set()
        for part in geom:
            for point in part:
                if point is None:
                    continue
                x, y, z = point.X, point.Y, point.Z
                key = (x, y, z)
                if key not in seen:
                    seen.add(key)
                    unique.append((x, y, z))
        return unique[:3] if len(unique) >= 3 else unique

    @staticmethod
    def _coordinate_system_metadata(spatial_ref, mode, custom_name, custom_epsg):
        if mode == "Omit coordinate system metadata":
            return None

        if mode == "Use custom metadata below":
            if not custom_name and not custom_epsg:
                return None
            return {
                "name": custom_name or "Custom coordinate system",
                "epsgCode": str(int(custom_epsg)) if custom_epsg else None,
                "desc": custom_name or "User supplied coordinate system metadata",
            }

        if spatial_ref is None or spatial_ref.name in (None, "", "Unknown"):
            return None

        meta = {"name": spatial_ref.name, "desc": spatial_ref.name, "epsgCode": None}
        try:
            if spatial_ref.factoryCode not in (None, 0):
                meta["epsgCode"] = str(spatial_ref.factoryCode)
        except Exception:
            pass
        return meta

    @staticmethod
    def _fmt(value, decimal_places):
        text = ("{0:." + str(decimal_places) + "f}").format(float(value))
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text if text else "0"

    def _point_text(self, x, y, z, coord_order, decimal_places):
        if coord_order.startswith("Northing"):
            values = (y, x, z)
        else:
            values = (x, y, z)
        return " ".join(self._fmt(v, decimal_places) for v in values)

    def _write_landxml(
        self,
        out_xml,
        surfaces,
        coord_order,
        linear_unit,
        z_unit,
        decimal_places,
        include_extents,
        logger,
    ):
        now = _datetime.datetime.now()
        root = _ET.Element(
            _q("LandXML"),
            {
                "version": "1.2",
                "date": now.strftime("%Y-%m-%d"),
                "time": now.strftime("%H:%M:%S"),
                "language": "English",
                "readOnly": "false",
                "{{{0}}}schemaLocation".format(XSI_NS): "http://www.landxml.org/schema/LandXML-1.2 http://www.landxml.org/schema/LandXML-1.2/LandXML-1.2.xsd",
            },
        )

        units = _ET.SubElement(root, _q("Units"))
        _ET.SubElement(
            units,
            _q("Metric"),
            {
                "linearUnit": linear_unit,
                "areaUnit": "squareMeter",
                "volumeUnit": "cubicMeter",
                "temperatureUnit": "celsius",
                "pressureUnit": "milliBars",
                "diameterUnit": "millimeter",
                "angularUnit": "decimal degrees",
                "directionUnit": "decimal degrees",
            },
        )

        project = _ET.SubElement(root, _q("Project"), {"name": "ArcGIS Pro TIN Export"})
        application = _ET.SubElement(
            root,
            _q("Application"),
            {
                "name": "Tin3DToLandXML ArcGIS Pro Python Toolbox",
                "manufacturer": "conterra / generated with ArcPy",
                "version": "1.2",
            },
        )
        application.set(
            "desc",
            "TIN to LandXML 1.2 surface exporter. Elevation values are written as supplied; declared linear unit: {0}; selected Z unit metadata: {1}.".format(linear_unit, z_unit),
        )

        written_coord_systems = set()
        for surface_data in surfaces:
            coord_meta = surface_data.get("coord_meta")
            if not coord_meta:
                continue
            key = (
                coord_meta.get("name"),
                coord_meta.get("desc"),
                coord_meta.get("epsgCode"),
            )
            if key in written_coord_systems:
                continue
            written_coord_systems.add(key)
            attrs = {"name": coord_meta.get("name") or "Coordinate System"}
            if coord_meta.get("desc"):
                attrs["desc"] = coord_meta["desc"]
            if coord_meta.get("epsgCode"):
                attrs["epsgCode"] = coord_meta["epsgCode"]
            _ET.SubElement(project, _q("CoordinateSystem"), attrs)

        surfaces_el = _ET.SubElement(root, _q("Surfaces"))
        for surface_data in surfaces:
            surface = _ET.SubElement(surfaces_el, _q("Surface"), {"name": surface_data["name"]})
            points = surface_data["points"]
            faces = surface_data["faces"]
            bounds = surface_data["bounds"]

            if include_extents:
                definition_attrs = {"surfType": "TIN", "area2DSurf": "0"}
            else:
                definition_attrs = {"surfType": "TIN"}
            definition = _ET.SubElement(surface, _q("Definition"), definition_attrs)

            if include_extents:
                ext = _ET.SubElement(definition, _q("Extents"))
                min_text = self._point_text(bounds["min_x"], bounds["min_y"], bounds["min_z"], coord_order, decimal_places)
                max_text = self._point_text(bounds["max_x"], bounds["max_y"], bounds["max_z"], coord_order, decimal_places)
                _ET.SubElement(ext, _q("Min"), {}).text = min_text
                _ET.SubElement(ext, _q("Max"), {}).text = max_text

            pnts = _ET.SubElement(definition, _q("Pnts"))
            for pid, x, y, z in points:
                p = _ET.SubElement(pnts, _q("P"), {"id": str(pid)})
                p.text = self._point_text(x, y, z, coord_order, decimal_places)

            face_el = _ET.SubElement(definition, _q("Faces"))
            for f1, f2, f3 in faces:
                f = _ET.SubElement(face_el, _q("F"))
                f.text = "{0} {1} {2}".format(f1, f2, f3)

        self._indent_xml(root)
        tree = _ET.ElementTree(root)
        tree.write(out_xml, encoding="utf-8", xml_declaration=True)
        logger.info("Wrote LandXML file: %s", out_xml)

    @staticmethod
    def _indent_xml(elem, level=0):
        indent = "\n" + level * "  "
        if len(elem):
            if not elem.text or not elem.text.strip():
                elem.text = indent + "  "
            for child in elem:
                Tin3DToLandXML._indent_xml(child, level + 1)
            if not elem.tail or not elem.tail.strip():
                elem.tail = indent
        else:
            if level and (not elem.tail or not elem.tail.strip()):
                elem.tail = indent


def _q(name):
    return "{{{0}}}{1}".format(LANDXML_NS, name)
