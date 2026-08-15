# SPDX-License-Identifier: AGPL-3.0-or-later
"""Ingest adapters.

Every source converts to the same thing - a list of lon/lat points plus a
radius and a layer - and then takes the identical path through segmentation,
interpolation and brush stamping. Manual drawing is not a special case; it
produces a synthetic LineString and goes through here too.

Populated in phase 1 (gpx, tcx) and phase 6 (live).
"""
