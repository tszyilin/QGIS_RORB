import geopandas as gpd

# Input and output shapefile paths
# input_shapefile = r"N:\PSM4872\Eng\11. Surface Water\Hydrology\_ref\shp\rorb_subareas_7851.shp"
# output_shapefile = r"N:\PSM4872\Eng\11. Surface Water\Hydrology\_ref\shp\rorb_subareas_7851_export.shp"
# input_shapefile = r"N:\PSM4872\Eng\11. Surface Water\Hydrology\_ref\shp\rorb_local_subareas_7851.shp"
# output_shapefile = r"N:\PSM4872\Eng\11. Surface Water\Hydrology\_ref\shp\rorb_local_subareas_7851_export.shp"
input_shapefile = r"N:\PSM4872\Eng\11. Surface Water\Hydrology\_ref\shp\pipeline_subcatchments_manual_28351_overlaps_removed.shp"
output_shapefile = r"N:\PSM4872\Eng\11. Surface Water\Hydrology\_ref\shp\pipeline_subareas_28351_export.shp"

# Read the shapefile
gdf = gpd.read_file(input_shapefile)

# Ensure the CRS is read from the .prj file
if gdf.crs is None:
    raise ValueError("The CRS of the shapefile could not be determined from the .prj file.")

# Check if CRS is geographic (degrees)
if gdf.crs.is_geographic:
    # Reproject to a projected CRS for accurate centroid calculation
    gdf_projected = gdf.to_crs(epsg=3857)  # Web Mercator projection
else:
    gdf_projected = gdf.copy()

# Compute centroids in projected CRS
gdf['centroid'] = gdf_projected.geometry.centroid

# Get y-coordinate (northing) of centroids
gdf['centroid_y'] = gdf['centroid'].y

# Sort by centroid_y (from south to north)
gdf_sorted = gdf.sort_values('centroid_y')

# Assign ids starting from 1
gdf_sorted['id'] = range(1, len(gdf_sorted) + 1)

# Drop temporary centroid columns
gdf_sorted = gdf_sorted.drop(columns=['centroid', 'centroid_y'])

# Save the updated shapefile, preserving the original CRS
gdf_sorted.to_file(output_shapefile, driver='ESRI Shapefile')

print(f"Updated shapefile saved to {output_shapefile}")
