import geopandas as gpd
import pandas as pd
import os

# Path to your original shapefile
original_shapefile_path = r"Q:\PSM2398\Eng\PSM2398.1 - Tailings Dam\4. Stage 1 Construction\6. Stage 1 Design Updates\2024 Stage 1 Northern Embankment Upgrade\Eng\shp\reach_rorb_slope.shp"

# Read the shapefile
gdf = gpd.read_file(original_shapefile_path)

# Check if the 's' attribute exists
if 's' not in gdf.columns:
    print("Attribute 's' not found in the shapefile.")
else:
    # Ensure 's' is a numeric data type
    gdf['s'] = pd.to_numeric(gdf['s'], errors='coerce')

    # Identify rows with negative values
    negative_values = gdf[gdf['s'] < 0]
    negative_values_count = len(negative_values)
    print(f"Found {negative_values_count} negative values in attribute 's'.")

    if negative_values_count > 0:
        # Print the rows with negative values including other attributes
        print("\nRows with negative values in 's' before replacement:")
        print(negative_values.drop(columns='geometry'))

        # Replace negative values with 0
        gdf.loc[gdf['s'] < 0, 's'] = 0
        print("\nNegative values have been replaced with 0.")

        # Print the updated rows
        updated_rows = gdf.loc[negative_values.index]
        print("\nRows after replacement:")
        print(updated_rows.drop(columns='geometry'))

        # Create new filename and path
        directory = os.path.dirname(original_shapefile_path)
        new_filename = "reach_rorb_final.shp"
        output_path = os.path.join(directory, new_filename)

        # Save the modified shapefile
        gdf.to_file(output_path)
        print(f"\nUpdated shapefile saved to {output_path}")
    else:
        print("No negative values found in attribute 's'. No changes made.")
