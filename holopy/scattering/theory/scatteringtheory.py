# Copyright 2011-2016, Vinothan N. Manoharan, Thomas G. Dimiduk,
# Rebecca W. Perry, Jerome Fung, Ryan McGorty, Anna Wang, Solomon Barkley
#
# This file is part of HoloPy.
#
# HoloPy is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# HoloPy is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with HoloPy.  If not, see <http://www.gnu.org/licenses/>.
"""
Base class for scattering theories.  Implements python-based
calc_intensity and calc_holo, based on subclass's calc_field
.. moduleauthor:: Jerome Fung <jerome.fung@post.harvard.edu>
.. moduleauthor:: Vinothan N. Manoharan <vnm@seas.harvard.edu>
.. moduleauthor:: Thomas G. Dimiduk <tdimiduk@physics.harvard.edu>
"""

import numpy as np
import xarray as xr
from warnings import warn
from holopy.core.holopy_object import HoloPyObject
from ..scatterer import Scatterers, Sphere
from ..errors import TheoryNotCompatibleError, MissingParameter
from ...core.metadata import vector, illumination, sphere_coords, primdim, update_metadata, clean_concat
from ...core.utils import dict_without, updated, ensure_array
try:
    from .mie_f import mieangfuncs
except ImportError:
    pass

def wavevec(a):
        return 2*np.pi/(a.illum_wavelen/a.medium_index)

def stack_spherical(a):
    if not 'r' in a:
        a['r']=[np.inf]*len(a['theta'])
    return np.vstack((a['r'],a['theta'],a['phi']))


class ScatteringTheory(HoloPyObject):
    """
    Defines common interface for all scattering theories.
    Notes
    -----
    A subclasses that do the work of computing scattering should do it by
    implementing _raw_fields and/or _raw_scat_matrs and (optionally)
    _raw_cross_sections. _raw_cross_sections is needed only for
    calc_cross_sections. Either of _raw_fields or _raw_scat_matrs will give you
    calc_holo, calc_field, and calc_intensity. Obviously calc_scat_matrix will
    only work if you implement _raw_cross_sections.
    So the simplest thing is to just implement _raw_scat_matrs. You only need to
    do _raw_fields there is a way to compute it more efficently and you care
    about that speed, or if it is easier and you don't care about matrices.
    """

    def _calc_field(self, scatterer, schema):
        """
        Calculate fields.  Implemented in derived classes only.
        Parameters
        ----------
        scatterer : :mod:`.scatterer` object
            (possibly composite) scatterer for which to compute scattering
        Returns
        -------
        e_field : :mod:`.VectorGrid`
            scattered electric field
        """
        def get_field(s):
            if isinstance(scatterer,Sphere) and scatterer.center is None:
                raise MissingParameter("center")
            positions = sphere_coords(schema, s.center, wavevec=wavevec(schema))
            field = np.vstack(self._raw_fields(stack_spherical(positions), s, medium_wavevec=wavevec(schema), medium_index=schema.medium_index, illum_polarization=schema.illum_polarization)).T
            phase = np.exp(-1j*wavevec(schema)*s.center[2])
            # TODO: fix and re-enable internal fields
            #if self._scatterer_overlaps_schema(scatterer, schema):
            #    inner = scatterer.contains(schema.positions.xyz())
            #    field[inner] = np.vstack(
            #        self._raw_internal_fields(positions[inner].T, s,
            #                                  optics)).T
            field *= phase
            dimstr=primdim(positions)
            coords = {key: (dimstr, val.values) for key, val in positions[dimstr].coords.items()}
            coords = updated(coords, {dimstr: positions[dimstr], vector: ['x', 'y', 'z']})
            field = xr.DataArray(field, dims=[dimstr, vector], coords = coords, attrs=schema.attrs)
            return field

        if len(ensure_array(schema.illum_wavelen)) > 1:
            field = []
            for illum in schema.illum_wavelen.illumination:
                field.append(self._calc_field(scatterer.select({illumination:illum}), update_metadata(schema,
                    illum_wavelen=ensure_array(schema.illum_wavelen.sel(illumination=illum).values)[0],
                    illum_polarization=ensure_array(schema.illum_polarization.sel(illumination=illum).values))))
            field = clean_concat(field, dim = schema.illum_wavelen.illumination)
        else:
            # See if we can handle the scatterer in one step
            if self._can_handle(scatterer):
                field = get_field(scatterer)
            elif isinstance(scatterer, Scatterers):
            # if it is a composite, try superposition
                scatterers = scatterer.get_component_list()
                field = get_field(scatterers[0])
                for s in scatterers[1:]:
                    field += get_field(s)
            else:
                raise TheoryNotCompatibleError(self, scatterer)

        return field

    def _calc_cross_sections(self, scatterer, medium_wavevec, medium_index, illum_polarization):
        raw_sections = self._raw_cross_sections(scatterer=scatterer,
                                                medium_wavevec=medium_wavevec,
                                                medium_index=medium_index,
                                                illum_polarization=illum_polarization)
        return xr.DataArray(raw_sections, dims=['cross_section'],
                            coords={'cross_section': ['scattering', 'absorbtion',
                                                      'extinction', 'assymetry']})

    def _calc_scat_matrix(self, scatterer, schema):
        """
        Compute scattering matrices for scatterer
        Parameters
        ----------
        scatterer : :mod:`holopy.scattering.scatterer` object
            (possibly composite) scatterer for which to compute scattering
        Returns
        -------
        scat_matr : :mod:`.Marray`
            Scattering matrices at specified positions
        Notes
        -----
        calc_* functions can be called on either a theory class or a theory
        object.  If called on a theory class, they use a default theory object
        which is correct for the vast majority of situations.  You only need to
        instantiate a theory object if it has adjustable parameters and you want
        to use non-default values.
        """
        positions = sphere_coords(schema, scatterer.center)
        scat_matrs = self._raw_scat_matrs(scatterer, stack_spherical(positions), medium_wavevec=wavevec(schema), medium_index=schema.medium_index)
        dimstr = primdim(positions)

        for coorstr in dict_without(positions, [dimstr]):
            positions[coorstr] = (dimstr, positions[coorstr])

        dims = ['Epar', 'Eperp']
        dims = [dimstr] + dims
        positions['Epar'] = ['S2', 'S3']
        positions['Eperp'] = ['S4', 'S1']

        return xr.DataArray(scat_matrs, dims=dims, coords=positions, attrs=schema.attrs)


    def _raw_fields(self, pos, scatterer, medium_wavevec, medium_index, illum_polarization):

        scat_matr = self._raw_scat_matrs(scatterer, pos, medium_wavevec=medium_wavevec, medium_index=medium_index)

        fields = np.zeros_like(pos.T, dtype = np.array(scat_matr).dtype)
        for i, point in enumerate(pos.T):
            kr, theta, phi = point
            escat_sph = mieangfuncs.calc_scat_field(kr, phi, scat_matr[i],
                                                    illum_polarization.values[:2])
            fields[i] = mieangfuncs.fieldstocart(escat_sph, theta, phi)
        return fields.T
