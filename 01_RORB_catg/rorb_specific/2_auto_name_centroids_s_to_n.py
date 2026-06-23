import geopandas as gpd
import string

# Input shapefile paths
subcatchments_shapefile = r"N:\PSM4872\Eng\11. Surface Water\Hydrology\_ref\shp\pipeline_subareas_28351_export.shp"
centroids_shapefile = r"N:\PSM4872\Eng\11. Surface Water\Hydrology\_ref\shp\pipeline_centroids_28351.shp"
output_centroids_shapefile = r"N:\PSM4872\Eng\11. Surface Water\Hydrology\_ref\shp\pipeline_centroids_28351_export.shp"

# Read the shapefiles
subcatchments = gpd.read_file(subcatchments_shapefile)
centroids = gpd.read_file(centroids_shapefile)

# Ensure both GeoDataFrames have valid CRS
if subcatchments.crs is None:
    raise ValueError("The CRS of the subcatchments shapefile could not be determined.")
if centroids.crs is None:
    raise ValueError("The CRS of the centroids shapefile could not be determined.")

# Ensure both GeoDataFrames have the same CRS
if subcatchments.crs != centroids.crs:
    centroids = centroids.to_crs(subcatchments.crs)

# Rename 'id' in subcatchments to avoid conflicts
subcatchments = subcatchments.rename(columns={'id': 'subcatch_id'})

# Spatial join using 'predicate' to avoid FutureWarning
centroids_with_subcatchments = gpd.sjoin(
    centroids, subcatchments[['subcatch_id', 'geometry']], how='left', predicate='within'
)

# Check if there is more than one centroid per polygon
points_per_polygon = centroids_with_subcatchments.groupby('subcatch_id').size()
if any(points_per_polygon > 1):
    print(f"Warning: Some subcatchments have more than one centroid point. Number of points processed does not match the number of polygons.")
else:
    print(f"Number of points processed matches the number of polygons.")

# Function to convert subcatchment id to uppercase letter(s)
def id_to_letter(subcatchment_id):
    try:
        index = int(subcatchment_id) - 1  # Adjust for zero-based indexing
        letters = string.ascii_uppercase
        base = len(letters)
        if index < base:
            return letters[index]
        else:
            # Calculate for IDs beyond 'Z'
            result = ''
            while index >= 0:
                result = letters[index % base] + result
                index = index // base - 1
            return result
    except (ValueError, TypeError):
        return None

# Update the 'id' attribute in centroids based on subcatchment
centroids_with_subcatchments['id'] = centroids_with_subcatchments['subcatch_id'].apply(id_to_letter)

# Add or ensure the 'fi' attribute exists, with a default value of 0.0
if 'fi' not in centroids_with_subcatchments.columns:
    centroids_with_subcatchments['fi'] = 0.0  # Default float value
else:
    centroids_with_subcatchments['fi'] = centroids_with_subcatchments['fi'].fillna(0.0).astype(float)

# Ensure the 'id' column remains as a string
centroids_with_subcatchments['id'] = centroids_with_subcatchments['id'].fillna('').astype(str)

# Drop temporary columns from spatial join
centroids_with_subcatchments = centroids_with_subcatchments.drop(columns=['subcatch_id', 'index_right'])

# Save the updated centroids shapefile
centroids_with_subcatchments.to_file(output_centroids_shapefile, driver='ESRI Shapefile')

print(f"Updated centroid shapefile saved to {output_centroids_shapefile}")
