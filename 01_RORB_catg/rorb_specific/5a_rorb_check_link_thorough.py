import geopandas as gpd
from shapely.geometry import Point
import pandas as pd
from collections import defaultdict

# Define the tolerance for spatial matching (adjust based on your data)
tolerance = 0.0001  # Adjust as needed based on your coordinate system

# Paths to your shapefiles (replace these with your actual file paths)
# point_layer1_path = r"N:\PSM4872\Eng\11. Surface Water\Hydrology\_ref\shp\rorb_centroid_node_7851_export.shp"
# point_layer2_path = r"N:\PSM4872\Eng\11. Surface Water\Hydrology\_ref\shp\rorb_confluence_node_7851_export.shp"
# line_layer_path = r"N:\PSM4872\Eng\11. Surface Water\Hydrology\_ref\shp\rorb_reach_7851_export.shp"
# point_layer1_path = r"N:\PSM4872\Eng\11. Surface Water\Hydrology\_ref\shp\rorb_local_centroid_node_7851_export.shp"
# point_layer2_path = r"N:\PSM4872\Eng\11. Surface Water\Hydrology\_ref\shp\rorb_local_confluence_node_7851_export.shp"
# line_layer_path = r"N:\PSM4872\Eng\11. Surface Water\Hydrology\_ref\shp\rorb_local_reach_7851_export.shp"
point_layer1_path = r"N:\PSM4872\Eng\11. Surface Water\Hydrology\_ref\shp\pipeline_centroids_28351_export.shp"
point_layer2_path = r"N:\PSM4872\Eng\11. Surface Water\Hydrology\_ref\shp\pipeline_confluence_28351_export.shp"
line_layer_path = r"N:\PSM4872\Eng\11. Surface Water\Hydrology\_ref\shp\pipeline_reach_28351.shp_v2_export.shp"


# Read the shapefiles
point_layer1 = gpd.read_file(point_layer1_path)
point_layer2 = gpd.read_file(point_layer2_path)
line_layer = gpd.read_file(line_layer_path)

# Ensure all layers use the same coordinate reference system (CRS)
crs = line_layer.crs
point_layer1 = point_layer1.to_crs(crs)
point_layer2 = point_layer2.to_crs(crs)

# Combine point layers into one GeoDataFrame
points = gpd.GeoDataFrame(pd.concat([point_layer1, point_layer2], ignore_index=True), crs=crs)

# Build spatial index for points
points_sindex = points.sindex

# Collect discrepancies
discrepancies = []

# Dictionary to track lines connected to each point 'id'
point_line_map = defaultdict(list)

for idx, line_feat in line_layer.iterrows():
    line_geom = line_feat.geometry

    # Extract start and end points of the line
    if line_geom.geom_type == 'LineString':
        coords = list(line_geom.coords)
    elif line_geom.geom_type == 'MultiLineString':
        # If it's a MultiLineString, use the first LineString
        coords = list(list(line_geom.geoms)[0].coords)
    else:
        print(f"Unsupported geometry type for line {line_feat['id']}")
        continue

    start_point = Point(coords[0])
    end_point = Point(coords[-1])


    # Function to find nearest point within tolerance
    def find_nearest_point(point):
        possible_matches_index = list(points_sindex.intersection(point.bounds))
        possible_matches = points.iloc[possible_matches_index]
        distances = possible_matches.geometry.distance(point)
        min_distance = distances.min()
        if min_distance <= tolerance:
            nearest_point_idx = distances.idxmin()
            return points.loc[nearest_point_idx]
        else:
            return None


    # Find matching features for start and end points
    start_feat = find_nearest_point(start_point)
    end_feat = find_nearest_point(end_point)

    if start_feat is None or end_feat is None:
        print(f"Could not find matching point features for line {line_feat['id']}")
        continue

    start_id_value = start_feat['id']
    end_id_value = end_feat['id']

    # Create the expected line 'id' by concatenating point 'id's with an underscore
    expected_line_id = f"{start_id_value}_{end_id_value}"
    line_id_value = line_feat['id']

    if line_id_value != expected_line_id:
        discrepancies.append({
            'line_index': idx,
            'line_id': line_feat['id'],
            'current_id': line_id_value,
            'expected_id': expected_line_id
        })
        print(f"Line ID mismatch for Line {line_feat['id']}: Found '{line_id_value}', Expected '{expected_line_id}'")

    # Record that this line is connected to these points
    point_line_map[start_id_value].append(line_id_value)
    point_line_map[end_id_value].append(line_id_value)

if discrepancies:
    print("\nDiscrepancies found:")
    for d in discrepancies:
        print(f"Line {d['line_id']}: Found '{d['current_id']}', Expected '{d['expected_id']}'")
else:
    print("No discrepancies found. All line 'id' attributes are correct.")

# --------------------------------------
# Additional Functionality:
# Check each point in both shapefiles and verify it is connected to at least one line.
# Print the point id, the connected line ids, and number of lines. Flag any points not connected.

print("\nChecking point-to-line connectivity...")

# Check point_layer1
print("\nPoints from first point layer:")
for idx, p in point_layer1.iterrows():
    pid = p['id']
    connected_lines = point_line_map[pid]
    if len(connected_lines) == 0:
        print(f"Point ID {pid}: No connected lines! [FLAG]")
    else:
        print(f"Point ID {pid}: Connected lines = {connected_lines}, Number of lines = {len(connected_lines)}")

# Check point_layer2
print("\nPoints from second point layer:")
for idx, p in point_layer2.iterrows():
    pid = p['id']
    connected_lines = point_line_map[pid]
    if len(connected_lines) == 0:
        print(f"Point ID {pid}: No connected lines! [FLAG]")
    else:
        print(f"Point ID {pid}: Connected lines = {connected_lines}, Number of lines = {len(connected_lines)}")
