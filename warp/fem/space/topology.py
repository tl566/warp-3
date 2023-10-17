import warp as wp

from warp.fem.types import ElementIndex
from warp.fem.geometry import Geometry
from warp.fem import cache


class SpaceTopology:
    """
    Interface class for defining the topology of a function space.

    The topology only considers the indices of the nodes in each element, and as such,
    the connectivity pattern of the function space.
    It does not specify the actual location of the nodes within the elements, or the valuation function.
    """

    dimension: int
    """Embedding dimension of the function space"""

    NODES_PER_ELEMENT: int
    """Number of interpolation nodes per element of the geometry.
    
    .. note:: This will change to be defined per-element in future versions
    """

    @wp.struct
    class TopologyArg:
        """Structure containing arguments to be passed to device functions"""

        _: int  # Work around some issues when passing multiple empty structs
        pass

    def __init__(self, geometry: Geometry, nodes_per_element: int):
        self._geometry = geometry
        self.dimension = geometry.dimension
        self.NODES_PER_ELEMENT = wp.constant(nodes_per_element)
        self.ElementArg = geometry.CellArg

    @property
    def geometry(self) -> Geometry:
        """Underlying geometry"""
        return self._geometry

    def node_count(self) -> int:
        """Number of nodes in the interpolation basis"""
        raise NotImplementedError

    def topo_arg_value(self, device) -> "TopologyArg":
        """Value of the topology argument structure to be passed to device functions"""
        return SpaceTopology.TopologyArg()

    @property
    def name(self):
        return f"{self.__class__.__name__}_{self.NODES_PER_ELEMENT}"

    def __str__(self):
        return self.name

    @staticmethod
    def element_node_index(
        geo_arg: "ElementArg", topo_arg: "TopologyArg", element_index: ElementIndex, node_index_in_elt: int
    ):
        """Global node index for a given node in a given element"""
        raise NotImplementedError

    def element_node_indices(self, temporary_store: cache.TemporaryStore = None, device=None) -> cache.Temporary:
        """Returns a temporary array containing the global index for each node of each element"""

        NODES_PER_ELEMENT = self.NODES_PER_ELEMENT

        @cache.dynamic_kernel(suffix=self.name)
        def fill_element_node_indices(
            geo_cell_arg: self.geometry.CellArg,
            topo_arg: self.TopologyArg,
            element_node_indices: wp.array2d(dtype=int),
        ):
            element_index = wp.tid()
            for n in range(NODES_PER_ELEMENT):
                element_node_indices[element_index, n] = self.element_node_index(
                    geo_cell_arg, topo_arg, element_index, n
                )

        element_node_indices = cache.borrow_temporary(
            temporary_store,
            shape=(self.geometry.cell_count(), NODES_PER_ELEMENT),
            dtype=int,
            device=device,
        )
        wp.launch(
            dim=element_node_indices.array.shape[0],
            kernel=fill_element_node_indices,
            inputs=[
                self.geometry.cell_arg_value(device=element_node_indices.array.device),
                self.topo_arg_value(device=element_node_indices.array.device),
                element_node_indices.array,
            ],
        )

        return element_node_indices

    # Interface generating trace space topology

    def trace(self) -> "TraceSpaceTopology":
        """Trace of the function space over lower-dimensional elements of the geometry"""

        return TraceSpaceTopology(self)

    def full_space_topology(self) -> "SpaceTopology":
        """Returns the full space topology from which this topology is derived"""
        return self

    def __eq__(self, other: "SpaceTopology") -> bool:
        """Checks whether two topologies are compatible"""
        return self.geometry == other.geometry and self.name == other.name

    def is_derived_from(self, other: "SpaceTopology") -> bool:
        """Checks whether two topologies are equal, or `self` is the trace of `other`"""
        if self.dimension == other.dimension:
            return self == other
        if self.dimension + 1 == other.dimension:
            return self.full_space_topology() == other
        return False


class TraceSpaceTopology(SpaceTopology):
    """Auto-generated trace topology defining the node indices associated to the geometry sides"""

    def __init__(self, topo: SpaceTopology):
        super().__init__(topo.geometry, 2 * topo.NODES_PER_ELEMENT)

        self._topo = topo
        self.dimension = topo.dimension - 1
        self.ElementArg = topo.geometry.SideArg

        self.TopologyArg = topo.TopologyArg
        self.topo_arg_value = topo.topo_arg_value

        self.inner_cell_index = self._make_inner_cell_index()
        self.outer_cell_index = self._make_outer_cell_index()
        self.neighbor_cell_index = self._make_neighbor_cell_index()

        self.element_node_index = self._make_element_node_index()

    def node_count(self) -> int:
        return self._topo.node_count()

    @property
    def name(self):
        return f"{self._topo.name}_Trace"

    def _make_inner_cell_index(self):
        NODES_PER_ELEMENT = self._topo.NODES_PER_ELEMENT

        @cache.dynamic_func(suffix=self.name)
        def inner_cell_index(args: self.geometry.SideArg, element_index: ElementIndex, node_index_in_elt: int):
            index_in_inner_cell = wp.select(node_index_in_elt < NODES_PER_ELEMENT, -1, node_index_in_elt)
            return self.geometry.side_inner_cell_index(args, element_index), index_in_inner_cell

        return inner_cell_index

    def _make_outer_cell_index(self):
        NODES_PER_ELEMENT = self._topo.NODES_PER_ELEMENT

        @cache.dynamic_func(suffix=self.name)
        def outer_cell_index(args: self.geometry.SideArg, element_index: ElementIndex, node_index_in_elt: int):
            return self.geometry.side_outer_cell_index(args, element_index), node_index_in_elt - NODES_PER_ELEMENT

        return outer_cell_index

    def _make_neighbor_cell_index(self):
        NODES_PER_ELEMENT = self._topo.NODES_PER_ELEMENT

        @cache.dynamic_func(suffix=self.name)
        def neighbor_cell_index(args: self.geometry.SideArg, element_index: ElementIndex, node_index_in_elt: int):
            if node_index_in_elt < NODES_PER_ELEMENT:
                return self.geometry.side_inner_cell_index(args, element_index), node_index_in_elt
            else:
                return (
                    self.geometry.side_outer_cell_index(args, element_index),
                    node_index_in_elt - NODES_PER_ELEMENT,
                )

        return neighbor_cell_index

    def _make_element_node_index(self):
        @cache.dynamic_func(suffix=self.name)
        def trace_element_node_index(
            geo_side_arg: self.geometry.SideArg,
            topo_arg: self._topo.TopologyArg,
            element_index: ElementIndex,
            node_index_in_elt: int,
        ):
            cell_index, index_in_cell = self.neighbor_cell_index(geo_side_arg, element_index, node_index_in_elt)

            geo_cell_arg = self.geometry.side_to_cell_arg(geo_side_arg)
            return self._topo.element_node_index(geo_cell_arg, topo_arg, cell_index, index_in_cell)

        return trace_element_node_index

    def full_space_topology(self) -> SpaceTopology:
        """Returns the full space topology from which this topology is derived"""
        return self._topo

    def __eq__(self, other: "TraceSpaceTopology") -> bool:
        return self._topo == other._topo


class DiscontinuousSpaceTopologyMixin:
    """Helper for defining discontinuous topologies (per-element nodes)"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.element_node_index = self._make_element_node_index()

    def node_count(self):
        return self.geometry.cell_count() * self.NODES_PER_ELEMENT

    def _make_element_node_index(self):
        NODES_PER_ELEMENT = self.NODES_PER_ELEMENT

        @cache.dynamic_func(suffix=self.name)
        def element_node_index(
            elt_arg: self.geometry.CellArg,
            topo_arg: self.TopologyArg,
            element_index: ElementIndex,
            node_index_in_elt: int,
        ):
            return NODES_PER_ELEMENT * element_index + node_index_in_elt

        return element_node_index