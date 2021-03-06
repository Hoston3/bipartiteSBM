#! /usr/bin/env python
# -*- coding: utf-8 -*-
#
# det_k_bisbm -- a python module for partitioning bipartite networks using the bipartite stochastic block model
#
# Copyright (C) 2016-2019 Tzu-Chi Yen <tzuchi@netscied.tw>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""``engines`` - Inference engines of the bipartite stochastic block model
----------------------------------------------------------------------------------

This module contains the wrappers for the binaries that identifies the large-scale network
structure via the statistical inference of the bipartite stochastic block model.

.. note::

   TODO.

"""
from engines.kl import KL
from engines.mcmc import MCMC

__all__ = ["KL", "MCMC"]
