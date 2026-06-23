import geopandas as gpd
import string

# Input and output shapefile paths
input_shapefile = r"N:\PSM4872\Eng\11. Surface Water\Hydrology\_ref\shp\pipeline_confluence_28351.shp"
output_shapefile = r"N:\PSM4872\Eng\11. Surface Water\Hydrology\_ref\shp\pipeline_confluence_28351_export.shp"

# Read the shapefile
gdf = gpd.read_file(input_shapefile)

# Ensure the GeoDataFrame has a valid CRS
if gdf.crs is None:
    raise ValueError("The CRS of the shapefile could not be determined from the .prj file.")

# Store the original CRS
original_crs = gdf.crs

# Check if CRS is geographic (degrees)
if gdf.crs.is_geographic:
    # Reproject to EPSG:28351 (GDA94 / MGA zone 51) for accurate coordinate measurements
    gdf_projected = gdf.to_crs(epsg=28351)
else:
    gdf_projected = gdf.copy()

# Compute y-coordinate (northing) of each point
gdf_projected['y_coord'] = gdf_projected.geometry.y

# Sort by y-coordinate from south to north
gdf_sorted = gdf_projected.sort_values('y_coord')

# Reset index after sorting
gdf_sorted = gdf_sorted.reset_index(drop=True)

# Function to generate lowercase letter IDs ('a', 'b', ..., 'z', 'aa', 'ab', ..., etc.)
def generate_lowercase_ids(num_ids):
    letters = string.ascii_lowercase
    base = len(letters)
    ids = []
    for i in range(num_ids):
        n = i
        result = ''
        while True:
            result = letters[n % base] + result
            n = n // base - 1
            if n < 0:
                break
        ids.append(result)
    return ids

# Generate IDs for all points
num_points = len(gdf_sorted)
id_list = generate_lowercase_ids(num_points)

# Assign IDs to the GeoDataFrame
gdf_sorted['id'] = id_list

# Ensure the 'out' attribute exists and is an integer with default value 0
if 'out' not in gdf_sorted.columns:
    gdf_sorted['out'] = 0  # Default integer value
else:
    gdf_sorted['out'] = gdf_sorted['out'].fillna(0).astype(int)

# Remove temporary 'y_coord' column
gdf_sorted = gdf_sorted.drop(columns=['y_coord'])

# Reproject back to the original CRS if it was reprojected
if gdf.crs.is_geographic:
    gdf_final = gdf_sorted.to_crs(original_crs)
else:
    gdf_final = gdf_sorted

# Preserve original attributes and geometry, update 'id' and 'out'
gdf_final = gdf_final[gdf.columns.tolist()].copy()  # Ensure it's a copy
gdf_final['id'] = gdf_sorted['id']                  # Update 'id' column with new IDs
gdf_final['out'] = gdf_sorted['out']                # Update 'out' column as an integer

# Save the updated shapefile with the original CRS
gdf_final.to_file(output_shapefile, driver='ESRI Shapefile')

print(f"Updated shapefile saved to {output_shapefile}")
