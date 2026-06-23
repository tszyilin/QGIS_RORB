import geopandas as gpd
from shapely.geometry import Point, LineString
import pandas as pd
import warnings

warnings.simplefilter(action='ignore', category=pd.errors.SettingWithCopyWarning)

# --------------------------------------------------------------------------------
# Input shapefile paths
# --------------------------------------------------------------------------------
centroids_shapefile = r"N:\PSM4872\Eng\11. Surface Water\Hydrology\_ref\shp\pipeline_centroids_28351_export.shp"
confluences_shapefile = r"N:\PSM4872\Eng\11. Surface Water\Hydrology\_ref\shp\pipeline_confluence_28351_export.shp"
reaches_shapefile = r"N:\PSM4872\Eng\11. Surface Water\Hydrology\_ref\shp\pipeline_reach_28351.shp"
output_reaches_shapefile = r"N:\PSM4872\Eng\11. Surface Water\Hydrology\_ref\shp\pipeline_reach_28351.shp_v2_export.shp"

# --------------------------------------------------------------------------------
# Read the shapefiles
# --------------------------------------------------------------------------------
centroids_gdf = gpd.read_file(centroids_shapefile)
confluences_gdf = gpd.read_file(confluences_shapefile)
reaches_gdf = gpd.read_file(reaches_shapefile)

# --------------------------------------------------------------------------------
# Check CRS
# --------------------------------------------------------------------------------
if centroids_gdf.crs is None or confluences_gdf.crs is None or reaches_gdf.crs is None:
    raise ValueError("One or more shapefiles have undefined CRS.")

if not (centroids_gdf.crs == confluences_gdf.crs == reaches_gdf.crs):
    centroids_gdf = centroids_gdf.to_crs(reaches_gdf.crs)
    confluences_gdf = confluences_gdf.to_crs(reaches_gdf.crs)

# --------------------------------------------------------------------------------
# Combine centroids and confluences into a single GeoDataFrame
# --------------------------------------------------------------------------------
nodes_gdf = pd.concat([centroids_gdf, confluences_gdf], ignore_index=True)
nodes_sindex = nodes_gdf.sindex

# --------------------------------------------------------------------------------
# Helper function to find nearest node
# --------------------------------------------------------------------------------
search_radius = 50  # Adjust as appropriate

def find_nearest_node(point_geom, nodes_gdf, nodes_sindex, search_radius):
    search_area = point_geom.buffer(search_radius)
    possible_matches_index = list(nodes_sindex.intersection(search_area.bounds))
    possible_matches = nodes_gdf.iloc[possible_matches_index]
    precise_matches = possible_matches[possible_matches.intersects(search_area)]
    if not precise_matches.empty:
        # Use assign instead of modifying in place
        precise_matches = precise_matches.assign(
            distance=precise_matches.geometry.distance(point_geom)
        )
        nearest_node = precise_matches.loc[precise_matches['distance'].idxmin()]
        return nearest_node['id']
    else:
        return None

# --------------------------------------------------------------------------------
# Create reach IDs
# --------------------------------------------------------------------------------
reach_ids = []
unnamed_reaches = []

for idx, row in reaches_gdf.iterrows():
    geom = row.geometry
    if geom.is_empty:
        reach_ids.append(None)
        unnamed_reaches.append(idx)
        continue
    elif isinstance(geom, LineString):
        start_point = Point(geom.coords[0])
        end_point = Point(geom.coords[-1])
    elif geom.geom_type == 'MultiLineString':
        line_parts = list(geom.geoms)
        if not line_parts:
            reach_ids.append(None)
            unnamed_reaches.append(idx)
            continue
        start_point = Point(line_parts[0].coords[0])
        end_point = Point(line_parts[-1].coords[-1])
    else:
        reach_ids.append(None)
        unnamed_reaches.append(idx)
        continue

    from_node_id = find_nearest_node(start_point, nodes_gdf, nodes_sindex, search_radius)
    to_node_id = find_nearest_node(end_point, nodes_gdf, nodes_sindex, search_radius)

    if from_node_id and to_node_id:
        reach_id = f"{from_node_id}_{to_node_id}"
    else:
        reach_id = None
        unnamed_reaches.append(idx)

    reach_ids.append(reach_id)

reaches_gdf['id'] = reach_ids

# --------------------------------------------------------------------------------
# Print any unnamed reaches
# --------------------------------------------------------------------------------
if unnamed_reaches:
    print("The following reaches could not be named (index in GeoDataFrame):")
    print(unnamed_reaches)
    print(reaches_gdf.loc[unnamed_reaches][['id', 'geometry']])

# --------------------------------------------------------------------------------
# Ensure 't', 's', and 'id' columns exist and fix types explicitly
# --------------------------------------------------------------------------------

# 1. 't' column: integer
if 't' not in reaches_gdf.columns:
    # If missing, just create it
    reaches_gdf['t'] = pd.Series([1]*len(reaches_gdf), dtype='int64')
else:
    # Convert to numeric first (this avoids silent downcasting during fillna)
    reaches_gdf['t'] = pd.to_numeric(reaches_gdf['t'], errors='coerce')
    # Fill missing values with 1
    reaches_gdf['t'] = reaches_gdf['t'].fillna(1)
    # Finally cast to int64
    reaches_gdf['t'] = reaches_gdf['t'].astype('int64')

# 2. 's' column: float
if 's' not in reaches_gdf.columns:
    reaches_gdf['s'] = pd.Series([0.0]*len(reaches_gdf), dtype=float)
else:
    reaches_gdf['s'] = pd.to_numeric(reaches_gdf['s'], errors='coerce')
    reaches_gdf['s'] = reaches_gdf['s'].fillna(0.0)
    reaches_gdf['s'] = reaches_gdf['s'].astype(float)

# 3. 'id' column: string
if 'id' not in reaches_gdf.columns:
    reaches_gdf['id'] = ''
else:
    # Fill missing with empty string
    reaches_gdf['id'] = reaches_gdf['id'].fillna('')
    # Cast to string
    reaches_gdf['id'] = reaches_gdf['id'].astype(str)

# --------------------------------------------------------------------------------
# Define the schema
# --------------------------------------------------------------------------------
schema = {
    'geometry': 'LineString',
    'properties': {
        't': 'int:10',
        's': 'float:10.3',
        'id': 'str:10'
    }
}

# --------------------------------------------------------------------------------
# Save the updated shapefile
# --------------------------------------------------------------------------------
reaches_gdf.to_file(output_reaches_shapefile, driver='ESRI Shapefile', schema=schema)

print(f"Updated reaches shapefile saved to {output_reaches_shapefile}")
