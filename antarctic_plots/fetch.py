# Copyright (c) 2022 The Antarctic-Plots Developers.
# Distributed under the terms of the MIT License.
# SPDX-License-Identifier: MIT
#
# This code is part of the package:
# Antarctic-plots (https://github.com/mdtanker/antarctic_plots)
#
import os
import shutil
from getpass import getpass
from pathlib import Path

import pandas as pd
import pooch
import pygmt
import requests
import xarray as xr
from pyproj import Transformer

from antarctic_plots import regions, utils


def resample_grid(
    grid,
    initial_spacing=None,
    initial_region=None,
    initial_registration=None,
    spacing=None,
    region=None,
    registration=None,
    **kwargs,
):
    # if initial values not given, extract from supplied grid
    if initial_spacing is None:
        initial_spacing = float(utils.get_grid_info(grid)[0])
    if initial_region is None:
        initial_region = utils.get_grid_info(grid)[1]
    if initial_registration is None:
        initial_registration = utils.get_grid_info(grid)[4]

    # if new values not gived, set equal to initial values
    if spacing is None:
        spacing = initial_spacing
    if region is None:
        region = initial_region
    if registration is None:
        registration = initial_registration

    # don't allow regions bigger than original region
    # if region > initial_region:
    #     print("requested region is larger than the original, returning the original ",
    #         f" region:{initial_region}.")
    #     region=initial_region

    # if all specs are same as orginal, return orginal
    rules = [
        spacing == initial_spacing,
        region == initial_region,
        registration == initial_registration,
    ]
    if all(rules):
        print("returning original grid")
        resampled = grid

    # if spacing is smaller, return resampled
    elif spacing < initial_spacing:
        print(
            f"Warning, requested spacing ({spacing}) is smaller than the original ",
            f"({initial_spacing}).",
        )
        cut = pygmt.grdcut(
            grid=grid,
            region=region,
        )
        resampled = pygmt.grdsample(
            grid=grid,
            region=pygmt.grdinfo(cut, spacing=f"{spacing}r")[2:-1],
            spacing=f"{spacing}+e",
            registration=registration,
        )

    # if spacing is larger, return filtered / resampled
    elif spacing > initial_spacing:
        print("spacing larger than original, filtering and resampling")
        filtered = pygmt.grdfilter(
            grid=grid,
            filter=f"g{spacing}",
            region=region,
            distance=kwargs.get("distance", "0"),
            # nans=kwargs.get('nans',"r"),
        )
        resampled = pygmt.grdsample(
            grid=filtered,
            region=pygmt.grdinfo(filtered, spacing=f"{spacing}r")[2:-1],
            spacing=spacing,
            registration=registration,
        )

    else:
        print("returning grid with new region and/or registration, same spacing")
        cut = pygmt.grdcut(
            grid=grid,
            region=region,
            extend="",
        )
        resampled = pygmt.grdsample(
            grid=grid,
            spacing=f"{spacing}+e",
            region=pygmt.grdinfo(cut, spacing=f"{spacing}r")[2:-1],
            registration=registration,
        )

    return resampled


class EarthDataDownloader:
    """
    Adapted from IcePack: https://github.com/icepack/icepack/blob/master/icepack/datasets.py  # noqa
    Either pulls login details from pre-set environment variables, or prompts user to
    input username and password.
    """

    def __init__(self):
        self._username = None
        self._password = None

    def _get_credentials(self):
        if self._username is None:
            username_env = os.environ.get("EARTHDATA_USERNAME")
            if username_env is None:
                self._username = input("EarthData username: ")
            else:
                self._username = username_env

        if self._password is None:
            password_env = os.environ.get("EARTHDATA_PASSWORD")
            if password_env is None:
                self._password = getpass("EarthData password: ")
            else:
                self._password = password_env

        return self._username, self._password

    def __call__(self, url, output_file, dataset):
        auth = self._get_credentials()
        downloader = pooch.HTTPDownloader(auth=auth, progressbar=True)
        try:
            login = requests.get(url)
            downloader(login.url, output_file, dataset)
        except requests.exceptions.HTTPError as error:
            if "Unauthorized" in str(error):
                pooch.get_logger().error("Wrong username/password!")
                self._username = None
                self._password = None
            raise error


def sample_shp(name: str) -> str:
    """
    Load the file path of sample shapefiles

    Parameters
    ----------
    name : str
        chose which sample shapefile to load, either 'Disco_deep_transect' or
        'Roosevelt_Island'

    Returns
    -------
    str
        file path as a string
    """
    path = pooch.retrieve(
        url=f"https://github.com/mdtanker/antarctic_plots/raw/main/data/{name}.zip",
        path=f"{pooch.os_cache('pooch')}/antarctic_plots/shapefiles",
        processor=pooch.Unzip(),
        known_hash=None,
    )
    file = [p for p in path if p.endswith(".shp")][0]
    return file


def ice_vel(
    plot: bool = False,
    info: bool = False,
    region=None,
    spacing=None,
    registration=None,
) -> xr.DataArray:
    """
    MEaSUREs Phase-Based Antarctica Ice Velocity Map, version 1:
    https://nsidc.org/data/nsidc-0754/versions/1#anchor-1
    Data part of https://doi.org/10.1029/2019GL083826

    Parameters
    ----------
    plot : bool, optional
        choose to plot grid, by default False
    info : bool, optional
        choose to print info on grid, by default False
    region : str or np.ndarray, optional
        GMT-format region to clip the loaded grid to, by default doesn't clip
    spacing : str or int, optional
        grid spacing to resample the loaded grid to, by default 10e3, original spacing
        is 450m
    registration : str, optional
        set either 'p' for pixel or 'g' for gridline registration, by default is 'g'

    Returns
    -------
    xr.DataArray
        Returns a calculated grid of ice velocity in meters/year.
    """

    # preprocessing for full, 450m resolution
    def preprocessing_fullres(fname, action, pooch):
        "Load the .nc file, calculate velocity magnitude, save it back"
        fname = Path(fname)
        # Rename to the file to ***_preprocessed.nc
        fname_processed = fname.with_stem(fname.stem + "_preprocessed_fullres")
        # Only recalculate if new download or the processed file doesn't exist yet
        if action in ("download", "update") or not fname_processed.exists():
            grid = xr.load_dataset(fname)
            processed = (grid.VX**2 + grid.VY**2) ** 0.5
            # Save to disk
            processed.to_netcdf(fname_processed)
        return str(fname_processed)

    # preprocessing for filtered 5k resolution
    def preprocessing_5k(fname, action, pooch):
        "Load the .nc file, calculate velocity magnitude, resample to 5k, save it back"
        fname = Path(fname)
        # Rename to the file to ***_preprocessed_5k.nc
        fname_processed = fname.with_stem(fname.stem + "_preprocessed_5k")
        # Only recalculate if new download or the processed file doesn't exist yet
        if action in ("download", "update") or not fname_processed.exists():
            grid = xr.load_dataset(fname)
            processed = (grid.VX**2 + grid.VY**2) ** 0.5
            processed_lowres = resample_grid(processed, spacing=5e3)
            # Save to disk
            processed_lowres.to_netcdf(fname_processed)
        return str(fname_processed)

    # determine which resolution of preprocessed grid to use
    if spacing < 5e3:
        preprocessor = preprocessing_fullres
        initial_region = [-2800000.0, 2799800.0, -2799800.0, 2800000.0]
        initial_spacing = 450
        initial_registration = "g"
    elif spacing >= 5e3:
        print("using preprocessed 5km grid since spacing is > 5km")
        preprocessor = preprocessing_5k
        initial_region = [-2800000.0, 2795000.0, -2795000.0, 2800000.0]
        initial_spacing = 5e3
        initial_registration = "g"

    if region is None:
        region = initial_region
    if spacing is None:
        spacing = initial_spacing
    if registration is None:
        registration = initial_registration

    # This is the path to the processed (magnitude) grid
    path = pooch.retrieve(
        url="https://n5eil01u.ecs.nsidc.org/MEASURES/NSIDC-0754.001/1996.01.01/antarctic_ice_vel_phase_map_v01.nc",  # noqa
        fname="measures_ice_vel_phase_map.nc",
        path=f"{pooch.os_cache('pooch')}/antarctic_plots/ice_velocity",
        downloader=EarthDataDownloader(),
        known_hash=None,
        progressbar=True,
        processor=preprocessor,
    )

    grid = xr.load_dataarray(path)

    resampled = resample_grid(
        grid,
        initial_spacing=initial_spacing,
        initial_region=initial_region,
        initial_registration=initial_registration,
        spacing=spacing,
        region=region,
        registration=registration,
    )

    if plot is True:
        resampled.plot(robust=True)
    if info is True:
        print(pygmt.grdinfo(resampled))

    return resampled


def modis_moa(
    version: int = 750,
) -> str:
    """
    Load the MODIS MoA imagery in either 750m or 125m resolutions.

    Parameters
    ----------
    version : int, optional
        choose between 750m or 125m resolutions, by default 750m

    Returns
    -------
    str
       filepath for either 750m or 125m MODIS MoA Imagery
    """
    if version == 125:
        path = pooch.retrieve(
            url="https://daacdata.apps.nsidc.org/pub/DATASETS/nsidc0593_moa2009_v02/geotiff/moa125_2009_hp1_v02.0.tif.gz",  # noqa
            fname="moa125.tif.gz",
            path=f"{pooch.os_cache('pooch')}/antarctic_plots/imagery",
            downloader=EarthDataDownloader(),
            processor=pooch.Decompress(method="gzip", name="moa125_2009_hp1_v02.0.tif"),
            known_hash=None,
            progressbar=True,
        )
    elif version == 750:
        path = pooch.retrieve(
            url="https://daacdata.apps.nsidc.org/pub/DATASETS/nsidc0593_moa2009_v02/geotiff/moa750_2009_hp1_v02.0.tif.gz",  # noqa
            fname="moa750.tif.gz",
            path=f"{pooch.os_cache('pooch')}/antarctic_plots/imagery",
            downloader=EarthDataDownloader(),
            processor=pooch.Decompress(method="gzip", name="moa750_2009_hp1_v02.0.tif"),
            known_hash=None,
            progressbar=True,
        )
    else:
        raise ValueError("invalid version string")

    return path


def imagery() -> xr.DataArray:
    """
    Load the file path of Antarctic imagery geotiff from LIMA:
    https://lima.usgs.gov/fullcontinent.php
    will replace with below once figured out login issue with pooch
    MODIS Mosaic of Antarctica: https://doi.org/10.5067/68TBT0CGJSOJ
    Assessed from https://daacdata.apps.nsidc.org/pub/DATASETS/nsidc0730_MEASURES_MOA2014_v01/geotiff/ # noqa

    Returns
    -------
    str
        file path
    """
    path = pooch.retrieve(
        url="https://lima.usgs.gov/tiff_90pct.zip",
        fname="lima.zip",
        path=f"{pooch.os_cache('pooch')}/antarctic_plots/imagery",
        processor=pooch.Unzip(),
        known_hash=None,
        progressbar=True,
    )
    image = [p for p in path if p.endswith(".tif")][0]

    return image


def groundingline() -> str:
    """
    Load the file path of a groundingline shapefile
    Antarctic groundingline shape file, from
    https://doi.pangaea.de/10.1594/PANGAEA.819147
    Supplement to Depoorter et al. 2013: https://doi.org/10.1038/nature12567

    Returns
    -------
    str
        file path
    """
    path = pooch.retrieve(
        url="https://doi.pangaea.de/10013/epic.42133.d001",
        fname="groundingline_depoorter_2013.d001",
        path=f"{pooch.os_cache('pooch')}/antarctic_plots/shapefiles",
        known_hash=None,
        processor=pooch.Unzip(),
        progressbar=True,
    )
    file = [p for p in path if p.endswith(".shp")][0]
    return file


def basement(
    plot: bool = False,
    info: bool = False,
    region=None,
    spacing=None,
    registration=None,
) -> xr.DataArray:
    """
    Load a grid of basement topography.
    Offshore and sub-Ross Ice Shelf basement topography.
    from Tankersley et al. 2022:
    https://onlinelibrary.wiley.com/doi/10.1029/2021GL097371
    offshore data from Lindeque et al. 2016: https://doi.org/10.1002/2016GC006401

    Parameters
    ----------
    plot : bool, optional
        plot the fetched grid, by default False
    info : bool, optional
        print info on the fetched grid, by default False

    Returns
    -------
    xr.DataArray
        dataarray of basement depths
    """

    # found with utils.get_grid_info()
    initial_region = [-3330000.0, 1900000.0, -3330000.0, 1850000.0]
    initial_spacing = 5e3
    initial_registration = "p"

    if region is None:
        region = initial_region
    if spacing is None:
        spacing = initial_spacing
    if registration is None:
        registration = initial_registration

    path = pooch.retrieve(
        url="https://download.pangaea.de/dataset/941238/files/Ross_Embayment_basement_filt.nc",  # noqa
        fname="basement.nc",
        path=f"{pooch.os_cache('pooch')}/antarctic_plots/basement",
        known_hash=None,
        progressbar=True,
    )

    grid = xr.load_dataarray(path)

    resampled = resample_grid(
        grid,
        initial_spacing,
        initial_region,
        initial_registration,
        spacing,
        region,
        registration,
    )

    if plot is True:
        resampled.plot(robust=True)
    if info is True:
        print(pygmt.grdinfo(resampled))

    return resampled


def bedmachine(
    layer: str,
    reference: str = "geoid",
    plot: bool = False,
    info: bool = False,
    region=None,
    spacing=None,
    registration=None,
) -> xr.DataArray:
    """
    Load BedMachine data,  from Morlighem et al. 2020:
    https://doi.org/10.1038/s41561-019-0510-8

    Accessed from NSIDC via https://nsidc.org/data/nsidc-0756/versions/1.
    Also available from
    https://github.com/ldeo-glaciology/pangeo-bedmachine/blob/master/load_plot_bedmachine.ipynb # noqa

    Parameters
    ----------
    layer : str
        choose which layer to fetch:
        'surface', 'thickness', 'bed', 'firn', 'geoid', 'mapping', 'mask', 'errbed',
        'source'; 'icebase' will give results of surface-thickness
    reference : str
        choose whether heights are referenced to 'geoid' (EIGEN-6C4) or 'ellipsoid'
        (WGS84), by default is 'geoid'
    plot : bool, optional
        choose to plot grid, by default False
    info : bool, optional
        choose to print info on grid, by default False
    region : str or np.ndarray, optional
        GMT-format region to clip the loaded grid to, by default doesn't clip
    spacing : str or int, optional
        grid spacing to resample the loaded grid to, by default 10e3

    Returns
    -------
    xr.DataArray
        Returns a loaded, and optional clip/resampled grid of Bedmachine.
    """

    # found with utils.get_grid_info()
    initial_region = [-3333000.0, 3333000.0, -3333000.0, 3333000.0]
    initial_spacing = 500
    initial_registration = "g"

    if region is None:
        region = initial_region
    if spacing is None:
        spacing = initial_spacing
    if registration is None:
        registration = initial_registration

    path = pooch.retrieve(
        url="https://n5eil01u.ecs.nsidc.org/MEASURES/NSIDC-0756.002/1970.01.01/BedMachineAntarctica_2020-07-15_v02.nc",  # noqa
        fname="bedmachine.nc",
        path=f"{pooch.os_cache('pooch')}/antarctic_plots/topography",
        downloader=EarthDataDownloader(),
        known_hash=None,
        progressbar=True,
    )

    if layer == "icebase":
        grid = xr.load_dataset(path)["surface"]
        surface = resample_grid(
            grid,
            initial_spacing=initial_spacing,
            initial_region=initial_region,
            initial_registration=initial_registration,
            spacing=spacing,
            region=region,
            registration=registration,
        )

        grid = xr.load_dataset(path)["thickness"]
        thickness = resample_grid(
            grid,
            initial_spacing=initial_spacing,
            initial_region=initial_region,
            initial_registration=initial_registration,
            spacing=spacing,
            region=region,
            registration=registration,
        )

        resampled = surface - thickness

    elif layer in [
        "surface",
        "thickness",
        "bed",
        "firn",
        "geoid",
        "mapping",
        "mask",
        "errbed",
        "source",
    ]:
        grid = xr.load_dataset(path)[layer]
        resampled = resample_grid(
            grid,
            initial_spacing=initial_spacing,
            initial_region=initial_region,
            initial_registration=initial_registration,
            spacing=spacing,
            region=region,
            registration=registration,
        )

    else:
        raise ValueError("invalid layer string")

    if reference == "ellipsoid" and layer != "thickness":
        geoid = xr.load_dataset(path)["geoid"]
        resampled_geoid = resample_grid(
            geoid,
            initial_spacing=initial_spacing,
            initial_region=initial_region,
            initial_registration=initial_registration,
            spacing=spacing,
            region=region,
            registration=registration,
        )

        final_grid = resampled + resampled_geoid

    elif reference not in ["ellipsoid", "geoid"]:
        raise ValueError("invalid reference string")

    else:
        final_grid = resampled

    if plot is True:
        final_grid.plot(robust=True)
    if info is True:
        print(pygmt.grdinfo(final_grid))

    return final_grid


def bedmap2(
    layer: str,
    reference: str = "geoid",
    plot: bool = False,
    info: bool = False,
    region=None,
    spacing=None,
    registration=None,
) -> xr.DataArray:
    """
    Load bedmap2 data. All grids are by default referenced to the g104c geoid. Use the
    'reference' parameter to convert to the ellipsoid.
    Note, nan's in surface grid are set to 0.
    from https://doi.org/10.5194/tc-7-375-2013.

    Parameters
    ----------
    layer : str
        choose which layer to fetch:
        'surface', 'thickness', 'bed', 'gl04c_geiod_to_WGS84', 'icebase' will give
        results of surface-thickness
    reference : str
        choose whether heights are referenced to 'geoid' (g104c) or 'ellipsoid'
        (WGS84), by default is 'geoid'
    plot : bool, optional
        choose to plot grid, by default False
    info : bool, optional
        choose to print info on grid, by default False
    region : str or np.ndarray, optional
        GMT-format region to clip the loaded grid to, by default doesn't clip
    spacing : str or int, optional
        grid spacing to resample the loaded grid to, by default 10e3

    Returns
    -------
    xr.DataArray
        Returns a loaded, and optional clip/resampled grid of Bedmap2.
    """
    # Declare initial grid values, found with utils.get_grid_info()
    # several of the layers have different values
    if layer == "lakemask_vostok":
        initial_region = [1189500.0, 1470500.0, -401500.0, -291500.0]
        initial_spacing = 1e3
        initial_registration = "p"

    elif layer == "thickness_uncertainty_5km":
        initial_region = [-3401500.0, 3403500.0, -3397500.0, 3397500.0]
        initial_spacing = 5e3
        initial_registration = "p"

    else:
        initial_region = [-3333500, 3333500, -3332500, 3332500]
        initial_spacing = 1e3
        initial_registration = "p"

    if region is None:
        region = initial_region
    if spacing is None:
        spacing = initial_spacing
    if registration is None:
        registration = initial_registration

    # retrieve the specified layer file
    path = pooch.retrieve(
        url="https://secure.antarctica.ac.uk/data/bedmap2/bedmap2_tiff.zip",
        fname="bedmap2_tiff.zip",
        path=f"{pooch.os_cache('pooch')}/antarctic_plots/topography",
        known_hash=None,
        processor=pooch.Unzip(),
        progressbar=True,
    )

    # calculate icebase as surface-thickness
    if layer == "icebase":
        fname = [p for p in path if p.endswith("surface.tif")][0]
        grid = xr.load_dataarray(fname).squeeze()
        surface = resample_grid(
            grid,
            initial_spacing=initial_spacing,
            initial_region=initial_region,
            initial_registration=initial_registration,
            spacing=spacing,
            region=region,
            registration=registration,
        )

        fname = [p for p in path if p.endswith("thickness.tif")][0]
        grid = xr.load_dataarray(fname).squeeze()
        thickness = resample_grid(
            grid,
            initial_spacing=initial_spacing,
            initial_region=initial_region,
            initial_registration=initial_registration,
            spacing=spacing,
            region=region,
            registration=registration,
        )

        # this changes the registration from pixel to gridline
        resampled = surface - thickness

    elif layer in [
        "bed",
        "coverage",
        "grounded_bed_uncertainty",
        "icemask_grounded_and_shelves",
        "lakemask_vostok",
        "rockmask",
        "surface",
        "thickness",
        "thickness_uncertainty_5km",
        "gl04c_geiod_to_WGS84",
    ]:

        fname = [p for p in path if p.endswith(f"{layer}.tif")][0]
        grid = xr.load_dataarray(fname).squeeze()
        resampled = resample_grid(
            grid,
            initial_spacing=initial_spacing,
            initial_region=initial_region,
            initial_registration=initial_registration,
            spacing=spacing,
            region=region,
            registration=registration,
        )

    else:
        raise ValueError("invalid layer string")

    # change layer elevation to be relative to the ellipsoid instead of the geoid
    if reference == "ellipsoid" and layer in [
        "surface",
        "icebase",
        "bed",
    ]:
        geoid_file = [p for p in path if p.endswith("gl04c_geiod_to_WGS84.tif")][0]
        geoid = xr.load_dataarray(geoid_file).squeeze()
        resampled_geoid = resample_grid(
            geoid,
            initial_spacing=initial_spacing,
            initial_region=initial_region,
            initial_registration=initial_registration,
            spacing=spacing,
            region=region,
            registration=registration,
        )

        final_grid = resampled + resampled_geoid

    elif reference not in ["ellipsoid", "geoid"]:
        raise ValueError("invalid reference string")

    else:
        final_grid = resampled

    # # replace nans with 0's
    # if layer in ["surface", "thickness"]:
    #     # pygmt.grdfill(final_grid, mode='c0') # doesn't work, maybe grid is too big
    #     # this changes the registration from pixel to gridline
    #     final_grid = final_grid.fillna(0)

    if plot is True:
        final_grid.plot(robust=True)
    if info is True:
        print(pygmt.grdinfo(final_grid))

    return final_grid


def deepbedmap(
    plot: bool = False,
    info: bool = False,
    region=None,
    spacing=None,
    registration=None,
) -> xr.DataArray:
    """
    Load DeepBedMap data,  from Leong and Horgan, 2020:
    https://doi.org/10.5194/tc-14-3687-2020
    Accessed from https://zenodo.org/record/4054246#.Ysy344RByp0

    Parameters
    ----------
    plot : bool, optional
        choose to plot grid, by default False
    info : bool, optional
        choose to print info on grid, by default False
    region : str or np.ndarray, optional
        GMT-format region to clip the loaded grid to, by default doesn't clip
    spacing : str or int, optional
        grid spacing to resample the loaded grid to, by default 10e3

    Returns
    -------
    xr.DataArray
        Returns a loaded, and optional clip/resampled grid of DeepBedMap.
    """

    # found with utils.get_grid_info()
    initial_region = [-2700000.0, 2800000.0, -2199750.0, 2299750.0]
    initial_spacing = 250
    initial_registration = "p"

    if region is None:
        region = initial_region
    if spacing is None:
        spacing = initial_spacing
    if registration is None:
        registration = initial_registration

    path = pooch.retrieve(
        url="https://zenodo.org/record/4054246/files/deepbedmap_dem.tif?download=1",
        fname="deepbedmap.tif",
        path=f"{pooch.os_cache('pooch')}/antarctic_plots/topography",
        known_hash=None,
        progressbar=True,
    )

    grid = xr.load_dataarray(path).squeeze()

    resampled = resample_grid(
        grid,
        initial_spacing,
        initial_region,
        initial_registration,
        spacing,
        region,
        registration,
    )

    if plot is True:
        resampled.plot(robust=True)
    if info is True:
        print(pygmt.grdinfo(resampled))

    return resampled


def gravity(
    version: str,
    plot: bool = False,
    info: bool = False,
    region=None,
    spacing=None,
    registration=None,
    **kwargs,
) -> xr.DataArray:
    """
    Loads 1 of 3 'versions' of Antarctic gravity grids.

    version='antgg'
    Antarctic-wide gravity data compilation of ground-based, airborne, and shipborne
    data, from Scheinert et al. 2016: New Antarctic gravity anomaly grid for enhanced
    geodetic and geophysical studies in Antarctica.
    DOI: https://doi.org/10.1002/2015GL067439
    Accessed from https://doi.pangaea.de/10.1594/PANGAEA.848168

    version='antgg-update'
    Preliminary compilation of Antarctica gravity and gravity gradient data.
    Updates on 2016 AntGG compilation.
    Accessed from https://ftp.space.dtu.dk/pub/RF/4D-ANTARCTICA/

    version='eigen'
    Earth gravity grid (eigen-6c4) at 10 arc-min resolution at 10km geometric height.
    orignally from https://dataservices.gfz-potsdam.de/icgem/showshort.php?id=escidoc:1119897 # noqa
    Accessed via the Fatiando data repository https://github.com/fatiando-data/earth-gravity-10arcmin # noqa

    Parameters
    ----------
    version : str
        choose which version of gravity data to fetch.

    plot : bool, optional
        choose to plot grid, by default False
    info : bool, optional
        choose to print info on grid, by default False
    region : str or np.ndarray, optional
        GMT-format region to clip the loaded grid to, by default doesn't clip
    spacing : str or int, optional
        grid spacing to resample the loaded grid to, by default 10e3

    Keyword Args
    ------------
    anomaly_type : str
            either 'FA' or 'BA', for free-air and bouguer anomalies, respectively.

    Returns
    -------
    xr.DataArray
        Returns a loaded, and optional clip/resampled grid of either free-air or
        Bouguer gravity anomalies.
    """
    anomaly_type = kwargs.get("anomaly_type", None)

    if version == "antgg":
        # found with utils.get_grid_info()
        initial_region = [-3330000.0, 3330000.0, -3330000.0, 3330000.0]
        initial_spacing = 10e3
        initial_registration = "g"

        if region is None:
            region = initial_region
        if spacing is None:
            spacing = initial_spacing
        if registration is None:
            registration = initial_registration

        path = pooch.retrieve(
            url="https://hs.pangaea.de/Maps/antgg2015/antgg2015.nc",
            fname="antgg.nc",
            path=f"{pooch.os_cache('pooch')}/antarctic_plots/gravity",
            known_hash=None,
            progressbar=True,
        )

        if anomaly_type == "FA":
            anomaly_type = "free_air_anomaly"
        elif anomaly_type == "BA":
            anomaly_type = "bouguer_anomaly"
        else:
            raise ValueError("invalid anomaly type")

        file = xr.load_dataset(path)[anomaly_type]

        # convert coordinates from km to m
        file_meters = file.copy()
        file_meters["x"] = file.x * 1000
        file_meters["y"] = file.y * 1000

        resampled = resample_grid(
            file_meters,
            initial_spacing,
            initial_region,
            initial_registration,
            spacing,
            region,
            registration,
        )

    elif version == "antgg-update":
        # found in documentation
        initial_region = [-3330000.0, 3330000.0, -3330000.0, 3330000.0]
        initial_spacing = 10e3
        initial_registration = "g"

        if region is None:
            region = initial_region
        if spacing is None:
            spacing = initial_spacing
        if registration is None:
            registration = initial_registration

        if anomaly_type not in ("FA", "BA"):
            raise ValueError("anomaly_type must be eith 'FA' or 'BA'")

        def preprocessing(fname, action, pooch2):
            "Unzip the folder, grid the .dat file, and save it back as a .nc"
            path = pooch.Unzip()(fname, action, pooch2)
            fname = [p for p in path if p.endswith(".dat")][0]
            fname = Path(fname)

            # Rename to the file to ***_preprocessed.nc
            fname_pre = fname.with_stem(fname.stem + f"_{anomaly_type}_preprocessed")
            fname_processed = fname_pre.with_suffix(".nc")

            # Only recalculate if new download or the processed file doesn't exist yet
            if action in ("download", "update") or not fname_processed.exists():
                # load data
                df = pd.read_csv(
                    fname,
                    delim_whitespace=True,
                    skiprows=3,
                    names=["id", "lat", "lon", "FA", "Err", "DG", "BA"],
                )
                # re-project to polar stereographic
                transformer = Transformer.from_crs("epsg:4326", "epsg:3031")
                df["x"], df["y"] = transformer.transform(
                    df.lat.tolist(), df.lon.tolist()
                )  # noqa

                # block-median and grid the data
                df = pygmt.blockmedian(
                    df[["x", "y", anomaly_type]],
                    spacing=initial_spacing,
                    region=initial_region,
                    registration=initial_registration,
                )
                processed = pygmt.surface(
                    data=df[["x", "y", anomaly_type]],
                    spacing=initial_spacing,
                    region=initial_region,
                    registration=initial_registration,
                    M="1c",
                )
                # Save to disk
                processed.to_netcdf(fname_processed)
            return str(fname_processed)

        path = pooch.retrieve(
            url="https://ftp.space.dtu.dk/pub/RF/4D-ANTARCTICA/ant4d_gravity.zip",
            fname="antgg_update.zip",
            path=f"{pooch.os_cache('pooch')}/antarctic_plots/gravity",
            known_hash=None,
            processor=preprocessing,
            progressbar=True,
        )

        grid = xr.load_dataarray(path)

        resampled = resample_grid(
            grid,
            initial_spacing,
            initial_region,
            initial_registration,
            spacing,
            region,
            registration,
        )

    elif version == "eigen":

        initial_region = [-3330000.0, 3330000.0, -3330000.0, 3330000.0]
        initial_spacing = 5e3
        initial_registration = "g"

        if region is None:
            region = initial_region
        if spacing is None:
            spacing = initial_spacing
        if registration is None:
            registration = initial_registration

        def preprocessing(fname, action, pooch):
            "Load the .nc file, reproject, and save it back"
            fname = Path(fname)
            # Rename to the file to ***_preprocessed.nc
            fname_processed = fname.with_stem(fname.stem + "_preprocessed")
            # Only recalculate if new download or the processed file doesn't exist yet
            if action in ("download", "update") or not fname_processed.exists():
                # load grid
                grid = xr.load_dataset(fname).gravity

                # reproject to polar stereographic
                grid2 = pygmt.grdproject(
                    grid,
                    projection="EPSG:3031",
                    spacing=initial_spacing,
                )
                # get just antarctica region
                processed = pygmt.grdsample(
                    grid2,
                    region=initial_region,
                    spacing=initial_spacing,
                    registration=initial_registration,
                )
                # Save to disk
                processed.to_netcdf(fname_processed)
            return str(fname_processed)

        path = pooch.retrieve(
            url="doi:10.5281/zenodo.5882207/earth-gravity-10arcmin.nc",
            fname="eigen.nc",
            path=f"{pooch.os_cache('pooch')}/antarctic_plots/gravity",
            known_hash=None,
            progressbar=True,
            processor=preprocessing,
        )

        grid = xr.load_dataarray(path)

        resampled = resample_grid(
            grid,
            initial_spacing,
            initial_region,
            initial_registration,
            spacing,
            region,
            registration,
        )

    else:
        raise ValueError("invalid version string")

    if plot is True:
        resampled.plot(robust=True)
    if info is True:
        print(pygmt.grdinfo(resampled))

    return resampled


def magnetics(
    version: str,
    plot: bool = False,
    info: bool = False,
    region=None,
    spacing=None,
    registration=None,
) -> xr.DataArray:
    """
    Load 1 of 3 'versions' of Antarctic magnetic anomaly grid.
    version='admap1'
    ADMAP-2001 magnetic anomaly compilation of Antarctica.
    https://admap.kongju.ac.kr/databases.html

    version='admap2'
    ADMAP2 magnetic anomaly compilation of Antarctica. Non-geosoft specific files
    provide from Sasha Golynsky.

    version='admap2_gdb'
    Geosoft-specific .gdb abridged files. Accessed from
    https://doi.pangaea.de/10.1594/PANGAEA.892722?format=html#download

    Parameters
    ----------
    version : str
        Either 'admap1', 'admap2', or 'admap2_gdb'
    plot : bool, optional
        choose to plot grid, by default False
    info : bool, optional
        choose to print info on grid, by default False
    region : str or np.ndarray, optional
        GMT-format region to clip the loaded grid to, by default doesn't clip
    spacing : str or int, optional
        grid spacing to resample the loaded grid to, by default 10e3

    Returns
    -------
    xr.DataArray
        Returns a loaded, and optional clip/resampled grid of magnetic anomalies.
    """

    if version == "admap1":
        # was in lat long, so just using standard values here
        initial_region = [-3330000.0, 3330000.0, -3330000.0, 3330000.0]
        initial_spacing = 5e3
        initial_registration = "g"

        if region is None:
            region = initial_region
        if spacing is None:
            spacing = initial_spacing
        if registration is None:
            registration = initial_registration

        def preprocessing(fname, action, pooch2):
            "Unzip the folder, grid the .dat file, and save it back as a .nc"
            fname = pooch.Unzip()(fname, action, pooch2)[0]
            fname = Path(fname)

            # Rename to the file to ***_preprocessed.nc
            fname_pre = fname.with_stem(fname.stem + "_preprocessed")
            fname_processed = fname_pre.with_suffix(".nc")

            # Only recalculate if new download or the processed file doesn't exist yet
            if action in ("download", "update") or not fname_processed.exists():
                # load data
                df = pd.read_csv(
                    fname,
                    delim_whitespace=True,
                    header=None,
                    names=["lat", "lon", "nT"],
                )

                # re-project to polar stereographic
                transformer = Transformer.from_crs("epsg:4326", "epsg:3031")
                df["x"], df["y"] = transformer.transform(
                    df.lat.tolist(), df.lon.tolist()
                )  # noqa

                # block-median and grid the data
                df = pygmt.blockmedian(
                    df[["x", "y", "nT"]],
                    spacing=initial_spacing,
                    region=initial_region,
                    registration=initial_registration,
                )
                processed = pygmt.surface(
                    data=df[["x", "y", "nT"]],
                    spacing=initial_spacing,
                    region=initial_region,
                    registration=initial_registration,
                    M="1c",
                )
                # Save to disk
                processed.to_netcdf(fname_processed)
            return str(fname_processed)

        path = pooch.retrieve(
            url="https://admap.kongju.ac.kr/admapdata/ant_new.zip",
            fname="admap1.zip",
            path=f"{pooch.os_cache('pooch')}/antarctic_plots/magnetics",
            known_hash=None,
            processor=preprocessing,
            progressbar=True,
        )

        grid = xr.load_dataarray(path)

        resampled = resample_grid(
            grid,
            initial_spacing,
            initial_region,
            initial_registration,
            spacing,
            region,
            registration,
        )

    # elif version == "admap2":
    #     path = "../data/ADMAP_2B_2017_R9_BAS_.tif"
    #     grd = xr.load_dataarray(path)
    #     grd = grd.squeeze()
    #     grd = pygmt.grdfilter(
    #         grid=grd,
    #         filter=f"g{spacing}",
    #         spacing=spacing,
    #         region=region,
    #         distance="0",
    #         nans="r",
    #         verbose="q",
    #     )

    elif version == "admap2_gdb":
        plot = False
        info = False
        path = pooch.retrieve(
            url="https://hs.pangaea.de/mag/airborne/Antarctica/ADMAP2A.zip",
            fname="admap2_gdb.zip",
            path=f"{pooch.os_cache('pooch')}/antarctic_plots/magnetics",
            known_hash=None,
            processor=pooch.Unzip(),
            progressbar=True,
        )
        resampled = path
    else:
        raise ValueError("invalid version string")

    if plot is True:
        resampled.plot(robust=True)
    if info is True:
        print(pygmt.grdinfo(resampled))

    return resampled


def ghf(
    version: str,
    plot: bool = False,
    info: bool = False,
    region=None,
    spacing=None,
    registration=None,
    **kwargs,
) -> xr.DataArray:
    """
    Load 1 of 6 'versions' of Antarctic geothermal heat flux data.

    version='an-2015'
    From At et al. 2015: emperature, lithosphere–asthenosphere boundary, and heat flux
    beneath the Antarctic Plate inferred from seismic velocities
    http://dx.doi.org/doi:10.1002/2015JB011917
    Accessed from http://www.seismolab.org/model/antarctica/lithosphere/index.html

    version='martos-2017'
    From Martos et al. 2017: Heat flux distribution of Antarctica unveiled. Geophysical
    Research Letters, 44(22), 11417-11426, https://doi.org/10.1002/2017GL075609
    Accessed from https://doi.pangaea.de/10.1594/PANGAEA.882503

    verion='shen-2020':
    From Shen et al. 2020; A Geothermal Heat Flux Map of Antarctica Empirically
    Constrained by Seismic Structure. https://doi.org/ 10.1029/2020GL086955
    Accessed from https://sites.google.com/view/weisen/research-products?authuser=0
    Used https://paperform.co/templates/apps/direct-download-link-google-drive/ to
    generate a direct download link from google drive page.
    https://drive.google.com/uc?export=download&id=1Fz7dAHTzPnlytuyRNctk6tAugCAjiqzR

    version='burton-johnson-2020'
    From Burton-Johnson et al. 2020: Review article: Geothermal heat flow in Antarctica:
    current and future directions, https://doi.org/10.5194/tc-14-3843-2020
    Accessed from supplementary material
    Choose for either of grid, or the point measurements

    version='losing-ebbing-2021'
    From Losing and Ebbing 2021: Predicting Geothermal Heat Flow in Antarctica With a
    Machine Learning Approach. Journal of Geophysical Research: Solid Earth, 126(6),
    https://doi.org/10.1029/2020JB021499
    Accessed from https://doi.pangaea.de/10.1594/PANGAEA.930237

    version='aq1'
    From Stal et al. 2021: Antarctic Geothermal Heat Flow Model: Aq1. DOI:
    https://doi.org/10.1029/2020GC009428
    Accessed from https://doi.pangaea.de/10.1594/PANGAEA.924857

    Parameters
    ----------
    version : str
        Either 'burton-johnson-2020', 'losing-ebbing-2021', 'aq1',
    plot : bool, optional
        choose to plot grid, by default False
    info : bool, optional
        choose to print info on grid, by default False
    region : str or np.ndarray, optional
        GMT-format region to clip the loaded grid to, by default doesn't clip
    spacing : int, optional
       grid spacing to resample the loaded grid to, by default spacing is read from
       downloaded files

    Returns
    -------
    xr.DataArray
         Returns a loaded, and optional clip/resampled grid of GHF data.
    """

    if version == "an-2015":
        # was in lat long, so just using standard values here
        initial_region = [-3330000.0, 3330000.0, -3330000.0, 3330000.0]
        initial_spacing = 5e3
        initial_registration = "g"

        if region is None:
            region = initial_region
        if spacing is None:
            spacing = initial_spacing
        if registration is None:
            registration = initial_registration

        def preprocessing(fname, action, pooch2):
            "Unzip the folder, reproject the .nc file, and save it back"
            fname = pooch.Untar()(fname, action, pooch2)[0]
            fname = Path(fname)

            # Rename to the file to ***_preprocessed.nc
            fname_pre = fname.with_stem(fname.stem + "_preprocessed")
            fname_processed = fname_pre.with_suffix(".nc")

            # Only recalculate if new download or the processed file doesn't exist yet
            if action in ("download", "update") or not fname_processed.exists():
                # load grid
                grid = xr.load_dataarray(fname)

                # reproject to polar stereographic
                grid2 = pygmt.grdproject(
                    grid,
                    projection="EPSG:3031",
                    spacing=initial_spacing,
                )
                # get just antarctica region
                processed = pygmt.grdsample(
                    grid2,
                    region=initial_region,
                    spacing=initial_spacing,
                    registration=initial_registration,
                )
                # Save to disk
                processed.to_netcdf(fname_processed)
            return str(fname_processed)

        path = pooch.retrieve(
            url="http://www.seismolab.org/model/antarctica/lithosphere/AN1-HF.tar.gz",
            fname="an_2015_.tar.gz",
            path=f"{pooch.os_cache('pooch')}/antarctic_plots/ghf",
            known_hash=None,
            progressbar=True,
            processor=preprocessing,
        )

        grid = xr.load_dataarray(path)

        resampled = resample_grid(
            grid,
            initial_spacing,
            initial_region,
            initial_registration,
            spacing,
            region,
            registration,
        )

    elif version == "martos-2017":
        # found from df.describe()
        initial_region = [-2535e3, 2715e3, -2130e3, 2220e3]
        initial_spacing = 15e3
        initial_registration = "g"

        if region is None:
            region = initial_region
        if spacing is None:
            spacing = initial_spacing
        if registration is None:
            registration = initial_registration

        def preprocessing(fname, action, pooch2):
            "Load the .xyz file, grid it, and save it back as a .nc"
            fname = Path(fname)

            # Rename to the file to ***_preprocessed.nc
            fname_pre = fname.with_stem(fname.stem + "_preprocessed")
            fname_processed = fname_pre.with_suffix(".nc")

            # Only recalculate if new download or the processed file doesn't exist yet
            if action in ("download", "update") or not fname_processed.exists():
                # load the data
                df = pd.read_csv(
                    fname, header=None, delim_whitespace=True, names=["x", "y", "GHF"]
                )

                # grid the data
                processed = pygmt.xyz2grd(
                    df,
                    region=initial_region,
                    registration=initial_registration,
                    spacing=initial_spacing,
                )

                # Save to disk
                processed.to_netcdf(fname_processed)
            return str(fname_processed)

        path = pooch.retrieve(
            url="https://store.pangaea.de/Publications/Martos-etal_2017/Antarctic_GHF.xyz",  # noqa
            fname="martos_2017.xyz",
            path=f"{pooch.os_cache('pooch')}/antarctic_plots/ghf",
            known_hash=None,
            progressbar=True,
            processor=preprocessing,
        )

        grid = xr.load_dataarray(path)

        resampled = resample_grid(
            grid,
            initial_spacing,
            initial_region,
            initial_registration,
            spacing,
            region,
            registration,
        )

    elif version == "burton-johnson-2020":
        # found from utils.get_grid_info(grid)
        initial_region = [-2543500.0, 2624500.0, -2121500.0, 2213500.0]
        initial_spacing = 17e3
        initial_registration = "p"

        if region is None:
            region = initial_region
        if spacing is None:
            spacing = initial_spacing
        if registration is None:
            registration = initial_registration

        path = pooch.retrieve(
            url="https://doi.org/10.5194/tc-14-3843-2020-supplement",
            fname="burton_johnson_2020.zip",
            path=f"{pooch.os_cache('pooch')}/antarctic_plots/ghf",
            known_hash=None,
            processor=pooch.Unzip(extract_dir="burton_johnson_2020"),
            progressbar=True,
        )

        if kwargs.get("points", False) is True:
            file = [p for p in path if p.endswith("V003.xlsx")][0]
            info = False
            plot = False
            # read the excel file with pandas
            GHF_points = pd.read_excel(file)

            # re-project the coordinates to Polar Stereographic
            transformer = Transformer.from_crs("epsg:4326", "epsg:3031")
            GHF_points["x"], GHF_points["y"] = transformer.transform(
                GHF_points["(1) Latitude"].tolist(),
                GHF_points["(2) Longitude"].tolist(),
            )  # noqa

            # rename
            GHF_points["GHF"] = GHF_points["(8) GHF (mW/m²)"]

            resampled = GHF_points

        elif kwargs.get("points", False) is False:
            file = [p for p in path if p.endswith("Mean.tif")][0]
            # pygmt gives issues when orginal filepath has spaces in it. To get around
            # this, we will copy the file into the parent directory.
            try:
                new_file = shutil.copyfile(
                    file,
                    f"{pooch.os_cache('pooch')}/antarctic_plots/ghf/burton_johnson_2020/Mean.tif",  # noqa
                )
            except shutil.SameFileError:
                new_file = file

            grid = xr.load_dataarray(new_file).squeeze()

            resampled = resample_grid(
                grid,
                initial_spacing,
                initial_region,
                initial_registration,
                spacing,
                region,
                registration,
            )

    elif version == "losing-ebbing-2021":
        # was in lat long, so just using standard values here
        initial_region = [-3330000.0, 3330000.0, -3330000.0, 3330000.0]
        initial_spacing = 5e3  # given as 0.5degrees, which is ~3.5km at the pole
        initial_registration = "g"

        if region is None:
            region = initial_region
        if spacing is None:
            spacing = initial_spacing
        if registration is None:
            registration = initial_registration

        def preprocessing(fname, action, pooch):
            "Load the .csv file, grid it, and save it back as a .nc"
            fname = Path(fname)

            # Rename to the file to ***_preprocessed.nc
            fname_pre = fname.with_stem(fname.stem + "_preprocessed")
            fname_processed = fname_pre.with_suffix(".nc")

            # Only recalculate if new download or the processed file doesn't exist yet
            if action in ("download", "update") or not fname_processed.exists():
                # load data
                df = pd.read_csv(fname)

                # block-median and grid the data
                df = pygmt.blockmedian(
                    df[["Lon", "Lat", "HF [mW/m2]"]],
                    spacing="30m",
                    coltypes="g",
                    region="AQ",
                    registration=initial_registration,
                )
                grid = pygmt.surface(
                    data=df[["Lon", "Lat", "HF [mW/m2]"]],
                    spacing="30m",
                    coltypes="g",
                    region="AQ",
                    registration=initial_registration,
                )

                # re-project to polar stereographic
                reprojected = pygmt.grdproject(
                    grid,
                    projection="EPSG:3031",
                    # spacing=initial_spacing,
                )

                processed = pygmt.grdsample(
                    reprojected,
                    spacing=initial_spacing,
                    region=initial_region,
                    registration=initial_registration,
                )

                # Save to disk
                processed.to_netcdf(fname_processed)
            return str(fname_processed)

        path = pooch.retrieve(
            url="https://download.pangaea.de/dataset/930237/files/HF_Min_Max_MaxAbs-1.csv",  # noqa
            fname="losing_ebbing_2021_ghf.csv",
            path=f"{pooch.os_cache('pooch')}/antarctic_plots/ghf",
            known_hash=None,
            progressbar=True,
            processor=preprocessing,
        )

        grid = xr.load_dataarray(path)

        resampled = resample_grid(
            grid,
            initial_spacing,
            initial_region,
            initial_registration,
            spacing,
            region,
            registration,
        )

    elif version == "aq1":
        # found from utils.get_grid_info(grid)
        initial_region = [-2800000.0, 2800000.0, -2800000.0, 2800000.0]
        initial_spacing = 20e3  # was actually 20071.6845878
        initial_registration = "g"

        if region is None:
            region = initial_region
        if spacing is None:
            spacing = initial_spacing
        if registration is None:
            registration = initial_registration

        path = pooch.retrieve(
            url="https://download.pangaea.de/dataset/924857/files/aq1_01_20.nc",
            fname="aq1.nc",
            path=f"{pooch.os_cache('pooch')}/antarctic_plots/ghf",
            known_hash=None,
            progressbar=True,
        )
        grid = xr.load_dataset(path)["Q"]

        grid = grid * 1000

        resampled = resample_grid(
            grid,
            initial_spacing,
            initial_region,
            initial_registration,
            spacing,
            region,
            registration,
        )

    elif version == "shen-2020":
        # was in lat long, so just using standard values here
        initial_region = regions.antarctica
        initial_spacing = 10e3
        initial_registration = "g"

        if region is None:
            region = initial_region
        if spacing is None:
            spacing = initial_spacing
        if registration is None:
            registration = initial_registration

        def preprocessing(fname, action, pooch2):
            "Load the .csv file, grid it, and save it back as a .nc"
            fname = Path(fname)

            # Rename to the file to ***_preprocessed.nc
            fname_pre = fname.with_stem(fname.stem + "_preprocessed")
            fname_processed = fname_pre.with_suffix(".nc")

            # Only recalculate if new download or the processed file doesn't exist yet
            if action in ("download", "update") or not fname_processed.exists():
                # load data
                df = pd.read_csv(
                    fname,
                    delim_whitespace=True,
                    header=None,
                    names=["lon", "lat", "GHF"],
                )
                # re-project to polar stereographic
                transformer = Transformer.from_crs("epsg:4326", "epsg:3031")
                df["x"], df["y"] = transformer.transform(
                    df.lat.tolist(), df.lon.tolist()
                )  # noqa

                # block-median and grid the data
                df = pygmt.blockmedian(
                    df[["x", "y", "GHF"]],
                    spacing=initial_spacing,
                    region=initial_region,
                    registration=initial_registration,
                )
                processed = pygmt.surface(
                    data=df[["x", "y", "GHF"]],
                    spacing=initial_spacing,
                    region=initial_region,
                    registration=initial_registration,
                    M="1c",
                )
                # Save to disk
                processed.to_netcdf(fname_processed)
            return str(fname_processed)

        path = pooch.retrieve(
            url="https://drive.google.com/uc?export=download&id=1Fz7dAHTzPnlytuyRNctk6tAugCAjiqzR",  # noqa
            fname="shen_2020_ghf.xyz",
            path=f"{pooch.os_cache('pooch')}/antarctic_plots/ghf",
            known_hash=None,
            processor=preprocessing,
            progressbar=True,
        )

        grid = xr.load_dataarray(path)

        resampled = resample_grid(
            grid,
            initial_spacing,
            initial_region,
            initial_registration,
            spacing,
            region,
            registration,
        )

    else:
        raise ValueError("invalid version string")

    if plot is True:
        resampled.plot(robust=True)
    if info is True:
        print(pygmt.grdinfo(resampled))

    return resampled


def gia(
    version: str,
    plot: bool = False,
    info: bool = False,
    region=None,
    spacing=None,
    registration=None,
) -> xr.DataArray:
    """
    Load 1 of 1 'versions' of Antarctic glacial isostatic adjustment grids.

    version='stal-2020'
    From Stal et al. 2020: Review article: The Antarctic crust and upper mantle: a
    flexible 3D model and framework for interdisciplinary research,
    https://doi.org/10.5194/tc-14-3843-2020
    Accessed from https://doi.org/10.5281/zenodo.4003423

    Parameters
    ----------
    version : str
        For now the only option is 'stal-2020',
    plot : bool, optional
        choose to plot grid, by default False
    info : bool, optional
        choose to print info on grid, by default False
    region : str or np.ndarray, optional
        GMT-format region to clip the loaded grid to, by default doesn't clip
    spacing : int, optional
       grid spacing to resample the loaded grid to, by default spacing is read from
       downloaded files

    Returns
    -------
    xr.DataArray
         Returns a loaded, and optional clip/resampled grid of GIA data.
    """

    if version == "stal-2020":
        # found from utils.get_grid_info(grid)
        initial_region = [-2800000.0, 2800000.0, -2800000.0, 2800000.0]
        initial_spacing = 10e3
        initial_registration = "p"

        if region is None:
            region = initial_region
        if spacing is None:
            spacing = initial_spacing
        if registration is None:
            registration = initial_registration

        path = pooch.retrieve(
            url="https://zenodo.org/record/4003423/files/ant_gia_dem_0.tiff?download=1",  # noqa
            fname="stal_2020_gia.tiff",
            path=f"{pooch.os_cache('pooch')}/antarctic_plots/gia",
            known_hash=None,
            progressbar=True,
        )
        grid = xr.load_dataarray(path).squeeze()

        resampled = resample_grid(
            grid,
            initial_spacing,
            initial_region,
            initial_registration,
            spacing,
            region,
            registration,
        )

    else:
        raise ValueError("invalid version string")

    if plot is True:
        resampled.plot(robust=True)
    if info is True:
        print(pygmt.grdinfo(resampled))

    return resampled


def crustal_thickness(
    version: str,
    plot: bool = False,
    info: bool = False,
    region=None,
    spacing=None,
    registration=None,
) -> xr.DataArray:
    """
    Load 1 of x 'versions' of Antarctic crustal thickness grids.

    version='shen-2018'
    Crustal thickness (excluding ice layer) from Shen et al. 2018: The crust and upper
    mantle structure of central and West Antarctica from Bayesian inversion of Rayleigh
    wave and receiver functions. https://doi.org/10.1029/2017JB015346
    Accessed from https://sites.google.com/view/weisen/research-products?authuser=0

    version='an-2015'
    Crustal thickness (distance from solid (ice and rock) top to Moho discontinuity)
    from An et al. 2015:  S-velocity Model and Inferred Moho Topography beneath the
    Antarctic Plate from Rayleigh Waves. J. Geophys. Res., 120(1),359–383,
    doi:10.1002/2014JB011332
    Accessed from http://www.seismolab.org/model/antarctica/lithosphere/index.html#an1s
    File is the AN1-CRUST model, paper states "Moho depths and crustal thicknesses
    referred to below are the distance from the solid surface to the Moho. We note that
    this definition of Moho depth is different from that in the compilation of AN-MOHO".
    Unclear, but seems moho depth is just negative of crustal thickness. Not sure if its
    to the ice surface or ice base.

    Parameters
    ----------
    version : str
        Either 'shen-2018',
        will add later: 'lamb-2020',  'an-2015', 'baranov', 'chaput', 'crust1',
        'szwillus', 'llubes', 'pappa', 'stal'
    plot : bool, optional
        choose to plot grid, by default False
    info : bool, optional
        choose to print info on grid, by default False
    region : str or np.ndarray, optional
        GMT-format region to clip the loaded grid to, by default doesn't clip
    spacing : int, optional
       grid spacing to resample the loaded grid to, by default spacing is read from
       downloaded files

    Returns
    -------
    xr.DataArray
         Returns a loaded, and optional clip/resampled grid of crustal thickness.
    """
    if version == "shen-2018":
        # was in lat long, so just using standard values here
        initial_region = regions.antarctica
        initial_spacing = 10e3  # given as 0.5degrees, which is ~3.5km at the pole
        initial_registration = "g"

        if region is None:
            region = initial_region
        if spacing is None:
            spacing = initial_spacing
        if registration is None:
            registration = initial_registration

        def preprocessing(fname, action, pooch2):
            "Load the .dat file, grid it, and save it back as a .nc"
            fname = Path(fname)

            # Rename to the file to ***_preprocessed.nc
            fname_pre = fname.with_stem("shen_2018_crustal_thickness_preprocessed")
            fname_processed = fname_pre.with_suffix(".nc")

            # Only recalculate if new download or the processed file doesn't exist yet
            if action in ("download", "update") or not fname_processed.exists():
                # load data
                df = pd.read_csv(
                    fname,
                    delim_whitespace=True,
                    header=None,
                    names=["lon", "lat", "thickness"],
                )
                # convert to meters
                df.thickness = df.thickness * 1000

                # re-project to polar stereographic
                transformer = Transformer.from_crs("epsg:4326", "epsg:3031")
                df["x"], df["y"] = transformer.transform(
                    df.lat.tolist(), df.lon.tolist()
                )  # noqa

                # block-median and grid the data
                df = pygmt.blockmedian(
                    df[["x", "y", "thickness"]],
                    spacing=initial_spacing,
                    region=initial_region,
                    registration=initial_registration,
                )
                processed = pygmt.surface(
                    data=df[["x", "y", "thickness"]],
                    spacing=initial_spacing,
                    region=initial_region,
                    registration=initial_registration,
                    M="1c",
                )
                # Save to disk
                processed.to_netcdf(fname_processed)
            return str(fname_processed)

        path = pooch.retrieve(
            url="https://weisen.wustl.edu/For_Comrades/for_self/moho.WCANT.dat",
            known_hash=None,
            fname="shen_2018_crustal_thickness.dat",
            path=f"{pooch.os_cache('pooch')}/antarctic_plots/crustal_thickness",
            processor=preprocessing,
            progressbar=True,
        )

        grid = xr.load_dataarray(path)

        resampled = resample_grid(
            grid,
            initial_spacing,
            initial_region,
            initial_registration,
            spacing,
            region,
            registration,
        )

    elif version == "an-2015":
        # was in lat long, so just using standard values here
        initial_region = [-3330000.0, 3330000.0, -3330000.0, 3330000.0]
        initial_spacing = 5e3
        initial_registration = "g"

        if region is None:
            region = initial_region
        if spacing is None:
            spacing = initial_spacing
        if registration is None:
            registration = initial_registration

        def preprocessing(fname, action, pooch2):
            "Unzip the folder, reproject the .nc file, and save it back"
            path = pooch.Untar(
                extract_dir="An_2015_crustal_thickness", members=["AN1-CRUST.grd"]
            )(fname, action, pooch2)
            fname = Path(path[0])
            # Rename to the file to ***_preprocessed.nc
            fname_processed = fname.with_stem(fname.stem + "_preprocessed")
            # Only recalculate if new download or the processed file doesn't exist yet
            if action in ("download", "update") or not fname_processed.exists():
                # load grid
                grid = xr.load_dataarray(fname)

                # convert to meters
                grid = grid * 1000

                # reproject to polar stereographic
                grid2 = pygmt.grdproject(
                    grid,
                    projection="EPSG:3031",
                    spacing=initial_spacing,
                )
                # get just antarctica region
                processed = pygmt.grdsample(
                    grid2,
                    region=initial_region,
                    spacing=initial_spacing,
                    registration=initial_registration,
                )
                # Save to disk
                processed.to_netcdf(fname_processed)
            return str(fname_processed)

        path = pooch.retrieve(
            url="http://www.seismolab.org/model/antarctica/lithosphere/AN1-CRUST.tar.gz",  # noqa
            fname="an_2015_crustal_thickness.tar.gz",
            path=f"{pooch.os_cache('pooch')}/antarctic_plots/crustal_thickness",
            known_hash=None,
            progressbar=True,
            processor=preprocessing,
        )

        grid = xr.load_dataarray(path)

        resampled = resample_grid(
            grid,
            initial_spacing,
            initial_region,
            initial_registration,
            spacing,
            region,
            registration,
        )

    else:
        raise ValueError("invalid version string")

    if plot is True:
        resampled.plot(robust=True)
    if info is True:
        print(pygmt.grdinfo(resampled))

    return resampled


def moho(
    version: str,
    plot: bool = False,
    info: bool = False,
    region=None,
    spacing=None,
    registration=None,
) -> xr.DataArray:
    """
    Load 1 of x 'versions' of Antarctic Moho depth grids.

    version='shen-2018'
    Depth to the Moho relative to the surface of solid earth (bottom of ice/ocean)
    from Shen et al. 2018: The crust and upper mantle structure of central and West
    Antarctica from Bayesian inversion of Rayleigh wave and receiver functions.
    https://doi.org/10.1029/2017JB015346
    Accessed from https://sites.google.com/view/weisen/research-products?authuser=0
    Appears to be almost identical to crustal thickness from Shen et al. 2018


    Parameters
    ----------
    version : str
        Either 'shen-2018', 'an-2015', 'pappa-2019',
        will add later: 'lamb-2020', 'baranov', 'chaput', 'crust1',
        'szwillus', 'llubes',
    plot : bool, optional
        choose to plot grid, by default False
    info : bool, optional
        choose to print info on grid, by default False
    region : str or np.ndarray, optional
        GMT-format region to clip the loaded grid to, by default doesn't clip
    spacing : int, optional
       grid spacing to resample the loaded grid to, by default spacing is read from
       downloaded files

    Returns
    -------
    xr.DataArray
         Returns a loaded, and optional clip/resampled grid of crustal thickness.
    """

    if version == "shen-2018":
        # was in lat long, so just using standard values here
        initial_region = regions.antarctica
        initial_spacing = 10e3  # given as 0.5degrees, which is ~3.5km at the pole
        initial_registration = "g"

        if region is None:
            region = initial_region
        if spacing is None:
            spacing = initial_spacing
        if registration is None:
            registration = initial_registration

        def preprocessing(fname, action, pooch2):
            "Load the .dat file, grid it, and save it back as a .nc"
            path = pooch.Untar(
                extract_dir="Shen_2018_moho", members=["WCANT_MODEL/moho.final.dat"]
            )(fname, action, pooch2)
            fname = [p for p in path if p.endswith("moho.final.dat")][0]
            fname = Path(fname)

            # Rename to the file to ***_preprocessed.nc
            fname_pre = fname.with_stem(fname.stem + "_preprocessed")
            fname_processed = fname_pre.with_suffix(".nc")

            # Only recalculate if new download or the processed file doesn't exist yet
            if action in ("download", "update") or not fname_processed.exists():
                # load data
                df = pd.read_csv(
                    fname,
                    delim_whitespace=True,
                    header=None,
                    names=["lon", "lat", "depth"],
                )
                # convert to meters
                df.depth = df.depth * -1000

                # re-project to polar stereographic
                transformer = Transformer.from_crs("epsg:4326", "epsg:3031")
                df["x"], df["y"] = transformer.transform(
                    df.lat.tolist(), df.lon.tolist()
                )  # noqa

                # block-median and grid the data
                df = pygmt.blockmedian(
                    df[["x", "y", "depth"]],
                    spacing=initial_spacing,
                    region=initial_region,
                    registration=initial_registration,
                )
                processed = pygmt.surface(
                    data=df[["x", "y", "depth"]],
                    spacing=initial_spacing,
                    region=initial_region,
                    registration=initial_registration,
                    M="1c",
                )
                # Save to disk
                processed.to_netcdf(fname_processed)
            return str(fname_processed)

        path = pooch.retrieve(
            url="https://drive.google.com/uc?export=download&id=1huoGe54GMNc-WxDAtDWYmYmwNIUGrmm0",  # noqa
            fname="shen_2018_moho.tar",
            path=f"{pooch.os_cache('pooch')}/antarctic_plots/moho",
            known_hash=None,
            progressbar=True,
            processor=preprocessing,
        )

        grid = xr.load_dataarray(path)

        resampled = resample_grid(
            grid,
            initial_spacing,
            initial_region,
            initial_registration,
            spacing,
            region,
            registration,
        )

    elif version == "an-2015":
        # was in lat long, so just using standard values here
        initial_region = [-3330000.0, 3330000.0, -3330000.0, 3330000.0]
        initial_spacing = 5e3
        initial_registration = "g"

        if region is None:
            region = initial_region
        if spacing is None:
            spacing = initial_spacing
        if registration is None:
            registration = initial_registration

        grid = crustal_thickness(version="an-2015") * -1

        resampled = resample_grid(
            grid,
            initial_spacing,
            initial_region,
            initial_registration,
            spacing,
            region,
            registration,
        )

    else:
        raise ValueError("invalid version string")

    if plot is True:
        resampled.plot(robust=True)
    if info is True:
        print(pygmt.grdinfo(resampled))

    return resampled
