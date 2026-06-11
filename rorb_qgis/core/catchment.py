import numpy as np
from .geometry import dist
from .attributes import Confluence


class Catchment:
    def __init__(self, confluences=None, basins=None, reaches=None):
        self._edges = reaches or []
        self._vertices = (confluences or []) + (basins or [])
        self._incidenceMatrixDS = []
        self._incidenceMatrixUS = []
        self._out = 0
        self._endSentinel = -1

    def connect(self):
        """Direct topology build: for each reach find its upstream and downstream node."""
        verts = self._vertices
        edges = self._edges
        nv = len(verts)
        ne = len(edges)

        conn = np.zeros((nv, ne), dtype=int)
        for j, edge in enumerate(edges):
            s = edge.getStart()
            e = edge.getEnd()
            min_s = min_e = 1e18
            cs = ce = 0
            for i, v in enumerate(verts):
                ds = dist(v, s)
                de = dist(v, e)
                if ds < min_s:
                    cs = i; min_s = ds
                if de < min_e:
                    ce = i; min_e = de
            conn[cs][j] = 1  # upstream end
            conn[ce][j] = 2  # downstream end

        for k, v in enumerate(verts):
            if isinstance(v, Confluence) and v.isOut:
                self._out = k
                break

        sentinel = self._endSentinel
        newDS = np.full((nv, ne), sentinel, dtype=int)
        newUS = np.full((nv, ne), sentinel, dtype=int)

        for j in range(ne):
            up = dn = sentinel
            for i in range(nv):
                if conn[i][j] == 1:
                    up = i
                elif conn[i][j] == 2:
                    dn = i
            if up != sentinel and dn != sentinel:
                newDS[up][j] = dn
                newUS[dn][j] = up

        self._incidenceMatrixDS = newDS
        self._incidenceMatrixUS = newUS
        return (newDS, newUS)
