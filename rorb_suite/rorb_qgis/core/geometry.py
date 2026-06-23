import math


class Point:
    def __init__(self, x=0.0, y=0.0):
        self._x = float(x)
        self._y = float(y)

    def coordinates(self):
        return (self._x, self._y)

    def __str__(self):
        return f"[{self._x:.3f}, {self._y:.3f}]"


def dist(a, b):
    ax, ay = a.coordinates()
    bx, by = b.coordinates()
    return math.sqrt((bx - ax) ** 2 + (by - ay) ** 2)


class Line:
    def __init__(self, vector=None):
        pts = [Point(t[0], t[1]) for t in (vector or [])]
        self._vector = pts
        self._end = max(len(pts) - 1, 0)
        total = 0.0
        for i in range(len(pts) - 1):
            x0, y0 = pts[i].coordinates()
            x1, y1 = pts[i + 1].coordinates()
            total += math.sqrt((x1 - x0) ** 2 + (y1 - y0) ** 2)
        self._length = total

    def length(self):
        return self._length

    def getStart(self):
        return self._vector[0]

    def getEnd(self):
        return self._vector[self._end]

    def __len__(self):
        return self._end

    def __getitem__(self, i):
        return self._vector[i]
