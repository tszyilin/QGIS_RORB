from enum import Enum
from .geometry import Point, Line


class Node(Point):
    def __init__(self, name="", x=0.0, y=0.0):
        super().__init__(x, y)
        self._name = str(name)

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, v):
        self._name = str(v)


class Basin(Node):
    def __init__(self, name="", x=0.0, y=0.0, area=0.0, fi=0.0):
        super().__init__(name, x, y)
        self._area = float(area)
        self._fi = float(fi)

    @property
    def area(self):
        return self._area

    @property
    def fi(self):
        return self._fi


class Confluence(Node):
    def __init__(self, name="", x=0.0, y=0.0, out=False):
        super().__init__(name, x, y)
        self._isOut = bool(out)

    @property
    def isOut(self):
        return self._isOut


class ReachType(Enum):
    NATURAL = 1
    UNLINED = 2
    LINED = 3
    DROWNED = 4


class Reach(Line):
    def __init__(self, name="", vector=None, rtype=None, slope=0.0):
        super().__init__(vector or [])
        self._name = str(name)
        self._type = rtype if rtype is not None else ReachType.NATURAL
        self._slope = float(slope)

    @property
    def name(self):
        return self._name

    @property
    def type(self):
        return self._type

    @property
    def slope(self):
        return self._slope

    def getSlope(self):
        return self._slope
