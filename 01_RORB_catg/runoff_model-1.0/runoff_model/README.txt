# Runoff Model Builder

The Runoff Model Builder suite of QGIS plugin generates RORB and URBS control vector files from GIS layers for hydrological modeling.

## Description

The Runoff Model Builder automates building RORB and URBS control vector files. It takes input GIS layers representing catchment reaches, basins, centroids, and confluences and generates a control vector file for the chosed hydrology model.

RORB and URBS are hydrological models used for catchment runoff modeling in Australia and other parts of the world.

## Features

- Generate RORB and URBS control vector files from GIS layers.
- Supports the same input layer structure between plugins.
- Integrated within the QGIS processing framework.

## Installation

1. Open QGIS
2. Go to Plugins > Manage and Install Plugins
3. Search for "Runoff Model Builder"
4. Install for all model builders.

## Dependencies

This plugin requires the [Pyromb](https://github.com/norman-tom/pyromb) library to be installed in the QGIS environment.

## Usage

1. Open QGIS
2. Go to Processing Toolbox
3. Find "Runoff Model Builder" in the algorithms list
4. Chosen hydrological model algorithm
5. Select your input layers:
   - Reach layer (line features)
   - Basin layer (polygon features)
   - Centroid layer (point features)
   - Confluence layer (point features)
6. Specify the output file location
7. Run the algorithm

## Requirements

- QGIS 3.22 or later
- pyromb library (https://github.com/norman-tom/pyromb) v0.3

## License

This plugin is licensed under the GNU General Public License v2.0 or later.

## Authors

Tom Norman

## Contributors 

Lindsay Millard