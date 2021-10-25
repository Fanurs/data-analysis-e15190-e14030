import inspect

import numpy as np
import pandas as pd

from e15190 import PROJECT_DIR
from e15190.utilities import geometry as geom
from e15190.utilities import tables

class Bar(geom.RectangularBar):
    def __init__(self, vertices, contain_pyrex=True):
        """Construct a Neutron Wall bar, either from Wall A or Wall B.

        This class only deals with individual bar. The Neutron Wall object is
        implemented in :py:class`Wall`.

        Parameters
        ----------
        vertices : ndarray of shape (8, 3)
            Exactly eight (x, y, z) lab coordinates measured in centimeter (cm).
            As long as all eight vertices are distinct, i.e. can properly define
            a cuboid, the order of vertices does not matter because PCA will be
            applied to automatically identify the 1st, 2nd and 3rd principal
            axes of the bar, which, for Neutron Wall bar, corresponds to the x,
            y and z directions in the local (bar) coordinate system.
        """
        super().__init__(vertices, make_vertices_dict=False)

        # 1st principal defines local-x, should be opposite to lab-x
        if np.dot(self.pca.components_[0], [-1, 0, 0]) < 0:
            self.pca.components_[0] *= -1

        # 2nd principal defines local-y, should nearly parallel to lab-y
        if np.dot(self.pca.components_[1], [0, 1, 0]) < 0:
            self.pca.components_[1] *= -1

        # 3rd principal defines local-z, direction is given by right-hand rule
        x_cross_y = np.cross(self.pca.components_[0], self.pca.components_[1])
        if np.dot(self.pca.components_[2], x_cross_y) < 0:
            self.pca.components_[2] *= -1

        self._make_vertices_dict()

        self.contain_pyrex = True # always starts with True
        """bool : Status of Pyrex thickness"""

        self.pyrex_thickness = 2.54 / 8 # cm
        """float : The thickness of the Pyrex (unit cm)
        
        Should be 0.125 inches or 0.3175 cm.
        """

        # remove Pyrex if requested
        if not contain_pyrex:
            self.remove_pyrex()
        else:
            # database always have included Pyrex, so no action is required
            pass
    
    @property
    def length(self):
        """Length of the bar.

        Should be around 76 inches without Pyrex.
        """
        return self.dimension(index=0)
    
    @property
    def height(self):
        """Height of the bar.

        Should be around 3.0 inches without Pyrex.
        """
        return self.dimension(index=1)

    @property
    def thickness(self):
        """Longitudinal thickness of the bar.

        Should be around 2.5 inches without Pyrex.
        """
        return self.dimension(index=2)
    
    def _modify_pyrex(self, mode):
        """To add or remove Pyrex thickness.

        Parameters
        ----------
        mode : 'add' or 'remove'
            If 'add', the Pyrex thickness will be added to the bar; if 'remove',
            the Pyrex thickness will be removed from the bar.

            An Exception will be raised if bar is already in the desired state.
        """
        if mode == 'remove':
            scalar = -1
        elif mode == 'add':
            scalar = +1
        
        # apply transformation
        self.loc_vertices = {
            iv: v + scalar * self.pyrex_thickness * np.array(iv)
            for iv, v in self.loc_vertices.items()
        }
        self.vertices = {
            iv: np.squeeze(self.to_lab_coordinates(v))
            for iv, v in self.loc_vertices.items()
        }

        # update status
        self.contain_pyrex = (not self.contain_pyrex)

    def remove_pyrex(self):
        """Remove Pyrex thickness from the bar."""
        if not self.contain_pyrex:
            raise Exception('Pyrex has already been removed')
        self._modify_pyrex('remove')
    
    def add_pyrex(self):
        """Add Pyrex thickness to the bar."""
        if self.contain_pyrex:
            raise Exception('Pyrex has already been added')
        self._modify_pyrex('add')

    def randomize_from_local_x(
        self,
        local_x,
        return_frame='lab',
        local_ynorm=[-0.5, 0.5],
        local_znorm=[-0.5, 0.5],
        random_seed=None,
    ):
        """Returns randomized point(s) from the given local x coordinate(s).

        This is useful for getting a uniform hit distribution within the bulk of
        the bar. Experimentally, we can only determine the local x coordinate
        for each hit.

        Parameters
        ----------
        local_x : float or list of floats
            The local x coordinate(s) of the point(s) in centimeters.
        return_frame : 'lab' or 'local', default 'lab'
            The frame of the returned point(s) in Cartesian coordinates.
        local_ynorm : float or 2-tuple of floats, default [-0.5, 0.5]
            The range of randomization in the local y coordinate. If float, no
            randomization is performed. Center is at `0.0`; the top surface is
            `+0.5`; the bottom surface is `-0.5`. Values outside this range will
            still be calculated, but those points will be outside the bar.
        local_znorm : float or 2-tuple of floats, default [-0.5, 0.5]
            The range of randomization in the local z coordinate. If float, no
            randomization is performed. Center is at `0.0`; the front surface is
            `+0.5`; the back surface is `-0.5`. Values outside this range will
            still be calculated, but those points will be outside the bar.
        random_seed : int, default None
            The random seed to be used. If None, randomization is
            non-reproducible.
        
        Returns
        -------
        randomized_coordinates : 3-tuple or list of 3-tuples
            The randomized point(s) in Cartesian coordinates.
        """
        rng = np.random.default_rng(random_seed)
        n_pts = len(local_x)

        if isinstance(local_ynorm, (int, float)):
            local_y = [local_ynorm * self.height] * n_pts
        else:
            local_y = rng.uniform(*local_ynorm, size=n_pts) * self.height

        if isinstance(local_znorm, (int, float)):
            local_z = [local_znorm * self.thickness] * n_pts
        else:
            local_z = rng.uniform(*local_znorm, size=n_pts) * self.thickness

        local_result = np.array([local_x, local_y, local_z]).T
        if return_frame == 'local':
            return local_result
        else:
            return self.to_lab_coordinates(local_result)

class Wall:
    def __init__(self, AB, contain_pyrex=True, refresh_from_inventor_readings=False):
        """Construct a neutron wall, A or B.

        A neutron wall is a collection of bars, ``self.bars``.

        Parameters
        ----------
        AB : 'A' or 'B'
            The wall to construct. Currently only 'B' is actively maintained and
            tested.
        contain_pyrex : bool, default True
            Whether the bars contain pyrex.
        refresh_from_inventor_readings : bool, default False
            If `True`, the geometry will be loaded from the inventor readings;
            if `False`, the geometry will be loaded from the existing database,
            stored in ``*.dat`` files.
        """
        self.AB = AB.upper()
        """'A' or 'B' : Neutron wall name in uppercase."""

        self.ab = self.AB.lower()
        """'a' or 'b' : Neutron wall name in lowercase."""

        self.database_dir = PROJECT_DIR / 'database/neutron_wall/geometry'
        """``pathlib.Path`` : ``PROJECT_DIR / 'database/neutron_wall/geometry'``"""

        self.path_inventor_readings = self.database_dir / f'inventor_readings_NW{self.AB}.txt'
        """``pathlib.Path`` : ``self.database_dir / f'inventor_readings_NW{self.AB}.txt'``"""

        self.path_vertices = self.database_dir / f'NW{self.AB}_vertices.dat'
        """``pathlib.Path`` : ``self.database_dir / f'NW{self.AB}_vertices.dat'``"""

        self.path_pca = self.database_dir / f'NW{self.AB}_pca.dat'
        """``pathlib.Path`` : ``self.database_dir / f'NW{self.AB}_pca.dat'``"""

        self.database = None
        """``pandas.DataFrame`` : Dataframe of vertices from all bars"""

        self.bars = None
        """``dict[int, Bar]`` : A collection of :py:class:`Bar` objects.
        
        The bottommost is bar 0, the topmost is bar 24. In the experiment, bar 0
        was not used because it was shadowed.
        """

        # if True, read in again from raw inventor readings
        self._refresh_from_inventor_readings = refresh_from_inventor_readings
        if self._refresh_from_inventor_readings:
            bars = self.read_from_inventor_readings(self.path_inventor_readings)
            self.save_vertices_to_database(self.AB, self.path_vertices, bars)
            self.save_pca_to_database(self.AB, self.path_pca, bars)

        # read in from database
        self.database = pd.read_csv(self.path_vertices, comment='#', delim_whitespace=True)
        index_names = [f'nw{self.ab}-bar', 'dir_x', 'dir_y', 'dir_z']
        self.database.set_index(index_names, drop=True, inplace=True)

        # construct a dictionary of bar objects
        bar_nums = self.database.index.get_level_values(f'nw{self.ab}-bar')
        self.bars = {
            b: Bar(self.database.loc[b][['x', 'y', 'z']], contain_pyrex=contain_pyrex)
            for b in bar_nums
        }
    
    @staticmethod
    def read_from_inventor_readings(filepath):
        """Reads in Inventor measurements and returns :py:class:`Bar` objects.

        Parameters
        ----------
        filepath : str or pathlib.Path
            The path to the file containing the Inventor measurements, e.g.
            :py:attr:`self.path_inventor_readings <path_inventor_readings>`.
        
        Returns
        -------
        bars : list
            A list of :py:class:`Bar` objects, sorted from bottom to top.
        """
        with open(filepath, 'r') as file:
            lines = file.readlines()
        
        # containers for process the lines
        bars_vertices = [] # to collect vertices of all bars
        vertex = [None] * 3
        vertices = [] # to collect all vertices of one particular bar

        for line in lines:
            sline = line.strip().split()

            # continue if this line does not contain measurement
            # else it will be in the format of, e.g.
            # X Position 200.0000 cm
            if 'cm' not in sline:
                continue

            coord_index = 'XYZ'.find(sline[0])
            if vertex[coord_index] is None:
                vertex[coord_index] = float(sline[2])
            else:
                raise Exception('ERROR: Trying to overwrite existing vertex component.')
            
            # append and reset if all 3 components of vertex have been read in
            if sum([ele is not None for ele in vertex]) == 3:
                vertices.append(vertex)
                vertex = [None] * 3
            
            # append and reset if all 8 vertices of a bar have been read in
            if len(vertices) == 8:
                bars_vertices.append(vertices)
                vertices = []
        
        # create Bar objects
        bars = [Bar(vertices) for vertices in bars_vertices]
        bars = sorted(bars, key=lambda bar: bar.pca.mean_[1]) # sort from bottom to top
        return bars
    
    @staticmethod
    def save_vertices_to_database(AB, filepath, bars):
        """Saves the vertices of the bars to database.

        Parameters
        ----------
        AB : 'A' or 'B'
            Neutron wall A or B.
        filepath : str or pathlib.Path
            Path to the database file, e.g. :py:attr:`self.path_vertices
            <path_vertices>`.
        bars : list of :py:class:`Bar` objects
            Sorted from bottom to top. The bottommost bar will be numbered 0;
            the topmost bar will be numbered 24.
        """
        # collect all vertices from all bars and save into a dataframe
        df = []
        for bar_num, bar_obj in enumerate(bars):
            for sign, vertex in bar_obj.vertices.items():
                df.append([bar_num, *sign, *vertex])
                
        df = pd.DataFrame(
            df,
            columns=[
                f'nw{AB.lower()}-bar',
                'dir_x', 'dir_y', 'dir_z',
                'x', 'y', 'z',
            ],
        )

        # save to database
        tables.to_fwf(
            df, filepath,
            comment='# measurement unit: cm',
            floatfmt=[
                '.0f',
                '.0f', '.0f', '.0f',
                '.4f', '.4f', '.4f',
            ],
        )

    @staticmethod
    def save_pca_to_database(AB, filepath, bars):
        """Save the principal components of the bars to database.

        Parameters
        ----------
        AB : str
            Neutron wall A or B.
        filepath : str or pathlib.Path
            Path to the database file, e.g. :py:attr:`self.path_pca <path_pca>`.
        bars : list of Bar objects
            Sorted from bottom to top. The bottommost bar will be numbered 0;
            the topmost bar will be numbered 24.
        """
        # collect all PCA components and means from all bars and save into a dataframe
        df = []
        for bar_num, bar_obj in enumerate(bars):
            if bar_obj.contain_pyrex:
                bar_obj.remove_pyrex()
            df.append([bar_num, 'L', *bar_obj.pca.mean_])
            for ic, component in enumerate(bar_obj.pca.components_):
                scaled_component = bar_obj.dimension(ic) * component
                df.append([bar_num, 'XYZ'[ic], *scaled_component])

        df = pd.DataFrame(
            df,
            columns=[
                f'nw{AB.lower()}-bar',
                'vector',
                'lab-x', 'lab-y', 'lab-z',
            ],
        )

        # save to database
        tables.to_fwf(
            df, filepath,
            comment=inspect.cleandoc('''
                # measurement unit: cm
                # vector:
                #   - L is NW bar center in lab frame
                #   - X, Y, Z are NW bar's principal components in lab frame w.r.t. to L,
                #     with the lengths equal to the bar's respective dimensions (without Pyrex)
            '''),
            floatfmt=[
                '.0f',
                's',
                '.5f', '.5f', '.5f',
            ],
        )
