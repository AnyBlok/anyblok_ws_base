# -*- coding: utf-8 -*-
# This file is a part of the AnyBlok / WMS Base project
#
#    Copyright (C) 2018 Georges Racinet <gracinet@anybox.fr>
#
# This Source Code Form is subject to the terms of the Mozilla Public License,
# v. 2.0. If a copy of the MPL was not distributed with this file,You can
# obtain one at http://mozilla.org/MPL/2.0/.
from sqlalchemy import orm
from sqlalchemy import or_
from sqlalchemy import and_
from sqlalchemy import not_
from sqlalchemy import func

from anyblok import Declarations
from anyblok.column import Integer
from anyblok.column import Text
from anyblok.column import Selection
from anyblok.relationship import Many2One
from anyblok_postgres.column import Jsonb
from .exceptions import (NodeStateError,
                         NodeChildrenStateError,
                         )

register = Declarations.register
Wms = Declarations.Model.Wms


@register(Wms.Inventory)
class Node:
    """Representation of the inventory of a subtree of containment hierarchy.

    For each Inventory, there's a tree of Inventory Nodes, each Node
    having one-to-many relationships to:

    - :class:`Inventory Lines <Line>` that together with its descendants',
      form the whole assessment of the contents of the Node's
      :attr:`location`
    - :class:`Inventory Actions <Action>` that encode the primary Operations
      that have to be executed to reconcile the database with the assessment.

    Each Node has a :attr:`location` under which the
    :attr:`locations <location>` of
    its children should be directly placed, but that doesn't mean each
    container visited by the inventory process has to be represented by a
    Node: instead, for each Inventory, the
    :attr:`locations <location>` of its leaf Nodes would ideally balance
    the amount of assessment work that can be done by one person in a
    continuous manner while keeping the size of the tree reasonible.

    Applications may want to override this Model to add user fields,
    representing who's in charge of a given node. The user would then either
    optionally take care of :meth:`splitting <split>` (issuing children)
    the Node and perform assesments that are not covered by children Nodes.

    This whole structure is designed so that assessment work can be
    distributed and reconciliation can be performed in parallel.
    """
    STATES = (
        ('draft', 'wms_inventory_state_draft'),
        ('full', 'wms_inventory_state_full'),
        ('computed', 'wms_inventory_state_computed'),
        ('pushed', 'wms_inventory_state_pushed'),
        ('reconciled', 'wms_inventory_state_reconciled'),
    )

    id = Integer(label="Identifier", primary_key=True)
    """Primary key."""

    state = Selection(selections=STATES,
                      nullable=False,
                      default='draft',
                      )
    """Node lifecycle

    - draft:
        the Node has been created, could still be split, but its
        :class:`lines <Line>` don't represent the full contents yet.
    - assessment:
        (TODO not there yet, do we need it?) assessment work
        has started.
    - full:
        all Physical Objects relevant to the Inventory that are below
        :attr:`location` are accounted for in the :class:`lines <Line>` of
        its Nodes or of its descendants. This implies in particular that
        none of the children Nodes is in prior states.
    - computed:
        all :class:`Actions <Action>` to reconcile the database with the
        assessment have been issued. It is still possible to simplify them
    - pushed:
        attached :class:`Actions` have been simplified, and the remaining
        ones have been pushed to the parent for further simplification
    - reconciled:
        all relevant Operations have been issued.
    """

    inventory = Many2One(model=Wms.Inventory,
                         index=True,
                         nullable=False)
    """The Inventory for which this Node has been created"""

    parent = Many2One(model='Model.Wms.Inventory.Node',
                      index=True)
    location = Many2One(model=Wms.PhysObj, nullable=False)

    def __repr__(self):
        return ("Wms.Inventory.Node(id={self.id}, "
                "inventory_id={self.inventory_id}, "
                "location_code={self.location.code!r})").format(self=self)

    __str__ = __repr__

    def __init__(self, parent=None, from_split=False, **fields):
        """Forbid creating subnodes if not from :meth:`split`

        Partially split Inventory Nodes are currently not consistent
        in their computation of reconciliation Actions.
        """
        if parent is not None and not from_split:
            raise NotImplementedError("Partially split Inventory Nodes are "
                                      "currently not supported. Please use "
                                      "Node.split() to create subnodes")
        super().__init__(parent=parent, **fields)

    @property
    def is_leaf(self):
        """(:class:`bool`): ``True`` if and only if the Node has no children.
        """
        return self.query().filter_by(parent=self).count() == 0

    def split(self):
        """Create a child Node for each container in :attr:`location`."""
        PhysObj = self.registry.Wms.PhysObj
        Avatar = PhysObj.Avatar
        ContainerType = orm.aliased(
            PhysObj.Type.query_behaviour('container', as_cte=True),
            name='container_type')
        subloc_query = (PhysObj.query()
                        .join(Avatar.obj)
                        .join(ContainerType,
                              ContainerType.c.id == PhysObj.type_id)
                        .filter(Avatar.state == 'present',
                                Avatar.location == self.location))
        return [self.insert(inventory=self.inventory,
                            from_split=True,
                            parent=self,
                            location=container)
                for container in subloc_query.all()]

    def phobj_filter(self, query):
        """Filter for :meth:`anyblok_wms_base.quantity_query()`

        This applies the rules of :attr:`considered_types`
        and :attr:`excluded_types`. Currently, this is not ready to handle
        big lists, the implementation does simple subqueries. Switching to
        JOINs would require more effort, which is not yet justified.

        Other selection rules could be added here.
        """
        PhysObj = self.registry.Wms.PhysObj
        POType = PhysObj.Type
        excluded_types = self.inventory.excluded_types
        considered_types = self.inventory.considered_types
        if excluded_types is not None:
            query = query.filter(
                not_(PhysObj.type_id.in_(
                    POType.query(POType.id)
                    .filter(POType.code.in_(excluded_types)))))
        if considered_types is not None:
            query = query.filter(
                PhysObj.type_id.in_(
                    POType.query(POType.id)
                    .filter(POType.code.in_(considered_types))))
        return query

    def compute_actions(self, recompute=False):
        """Create actions to reconcile database with assessment.

        :param bool recompute: if ``True``, can be applied even if
                               :attr:`state` is already ``computed``
                               but no later

        This Node's :class:`Lines <Line>` are compared with the Avatars in the
        ``present`` state that are under :attr:`location`.
        Unmatched quantities in Lines give rise to
        :class:`Actions <.action.Action>` with ``type='app'``,
        whereas unmatched quantities in Avatars give rise
        to :class:`Actions <.action.Action>` with ``type='disp'``.

        Nodes are assumed to be either leafs or fully split:
        if the Node is not a leaf, then only Avatars directly pointing
        to its :attr:`location` will be considered. By contrast, the whole
        subhierarchy of container objects under :attr:`location` will be
        considered for leaf Nodes.

        Finally, this method doesn't issue :class:`Actions <.action.Action>`
        with ``type='telep'``: this is done in subsequent simplification steps,
        see for instance :meth:`compute_push_actions`.

        *Implementation and performance details*:

        Internally, this uses an SQL query that's quite heavy:

        - recursive CTE for the sublocations
        - that's joined with Avatar and PhysObj to extract quantities
          and information (type, code, properties)
        - on top of that, full outer join with Inventory.Line

        but it has advantages:

        - works uniformely in the three cases:

          + no Inventory.Line matching a given Avatar
          + no Avatar matching a given Inventory.Line
          + a given Inventory.Line has matching Avatars, but the counts
            don't match
        - minimizes round-trip to the database
        - minimizes Python side processing
        """
        state = self.state
        if state in ('draft', 'assessment'):
            raise NodeStateError(
                self, "Can't compute actions on Node id={id} "
                "whose state {state!r} is less than 'full'")

        if state in ('computed', 'pushed', 'reconciled'):
            if recompute:
                self.clear_actions()
            else:
                raise NodeStateError(self, "Can't compute actions on "
                                     "Node id={id} (state={state!r}) "
                                     "that's already past the 'full' state'")
        cls = self.__class__
        non_ready_children = (cls.query()
                              .filter(cls.parent == self,
                                      cls.state != 'pushed')
                              .all())
        if non_ready_children:
            raise NodeChildrenStateError(
                self, non_ready_children,
                "This {node} has some non children "
                "who didn't push their Actions to it yet: "
                "{children_states!r}")

        PhysObj = self.registry.Wms.PhysObj
        POType = PhysObj.Type
        Avatar = PhysObj.Avatar
        Inventory = self.registry.Wms.Inventory
        Line = Inventory.Line
        Action = Inventory.Action

        cols = (Avatar.location_id, PhysObj.code, PhysObj.type_id)
        quantity_query = self.registry.Wms.quantity_query
        existing_phobjs = (quantity_query(location=self.location,
                                          location_recurse=self.is_leaf,
                                          additional_filter=self.phobj_filter)
                           .add_columns(*cols).group_by(*cols)
                           .subquery())

        node_lines = (Line.query(Line.quantity,
                                 Line.location_id,
                                 Line.type_id, Line.code, Line.properties)
                      .filter_by(node=self))

        considered_types = self.inventory.considered_types
        if considered_types is not None:
            node_lines = node_lines.filter(
                Line.type_id.in_(
                    POType.query(POType.id)
                    .filter(POType.code.in_(considered_types))))
        excluded_types = self.inventory.excluded_types
        if excluded_types is not None:
            node_lines = node_lines.filter(
                not_(Line.type_id.in_(
                    POType.query(POType.id)
                    .filter(POType.code.in_(excluded_types)))))
        node_lines = node_lines.subquery()

        comp_query = (
            self.registry.query(node_lines)
            .join(existing_phobjs,
                  # multiple criteria to join on the subquery would fail,
                  # complaining of lack of foreign key (SQLA bug maybe)?
                  # but it works with and_()
                  and_(node_lines.c.type_id == existing_phobjs.c.type_id,
                       node_lines.c.location_id == (existing_phobjs.c
                                                    .location_id),
                       or_(node_lines.c.code == existing_phobjs.c.code,
                           and_(existing_phobjs.c.code.is_(None),
                                node_lines.c.code.is_(None)))),
                  full=True)
            .filter(func.coalesce(existing_phobjs.c.qty, 0) !=
                    func.coalesce(node_lines.c.quantity, 0))
            .add_columns(func.coalesce(existing_phobjs.c.qty, 0)
                         .label('phobj_qty'),
                         # these columns are useful only if lines is None:
                         existing_phobjs.c.location_id.label('phobj_loc'),
                         existing_phobjs.c.type_id.label('phobj_type'),
                         existing_phobjs.c.code.label('phobj_code'),
                         ))

        for row in comp_query.all():
            line_qty = row[0]
            phobj_count = row[5]
            if line_qty is None:
                Action.insert(node=self,
                              type='disp',
                              quantity=phobj_count,
                              location=PhysObj.query().get(row[6]),
                              physobj_type=POType.query().get(row[7]),
                              physobj_code=row[8],
                              )
                continue

            diff_qty = phobj_count - line_qty
            fields = dict(node=self,
                          location_id=row[1],
                          physobj_type_id=row[2],
                          physobj_code=row[3],
                          physobj_properties=row[4])

            # the query is tailored so that diff_qty is never 0
            if diff_qty > 0:
                Action.insert(type='disp', quantity=diff_qty, **fields)
            else:
                Action.insert(type='app', quantity=-diff_qty, **fields)

        self.state = 'computed'

    def clear_actions(self):
        (self.registry.Wms.Inventory.Action.query()
         .filter_by(node=self)
         .delete(synchronize_session='fetch'))

    def compute_push_actions(self):
        """Compute and simplify actions, push unsimplifable ones to the parent

        The actions needed for reconcilation are first
        :meth:`computed <compute_actions>`, then
        simplified by matching apparitions with disparitions
        to issue teleportations.
        The remaining apparitions and disparitions are pushed to the parent
        node for further simplification until we reach the top.

        Pushing up to the parent may seem heavy, but it allows to split
        the whole reconciliation (with simplification) work into separate
        steps.

        For big inventories, the caller of this method would typically
        commit for each Node.
        For really big inventories, the work could be split up between
        different processes.
        """
        Action = self.registry.Wms.Inventory.Action
        self.compute_actions()
        Action.simplify(self)
        self.state = 'pushed'

        if self.parent is None:
            return
        Action = self.registry.Wms.Inventory.Action
        (Action.query()
         .filter(Action.type.in_(('app', 'disp')),
                 Action.node == self)
         .update(dict(node_id=self.parent.id),
                 synchronize_session='fetch'))

    def recurse_compute_push_actions(self):
        """Recursion along the whole tree in one shot.

        This is not recommended for big inventories, as it will lead to
        one huge transaction.
        """
        cls = self.__class__
        non_ready_children = (cls.query()
                              .filter(cls.parent == self,
                                      cls.state.in_(('draft', 'assessment')))
                              .all())
        if non_ready_children:
            raise NodeChildrenStateError(
                self, non_ready_children,
                "This {node} has some children "
                "which didn't compute their Actions yet: {children_states!r}")
        for child in self.query().filter_by(parent=self, state='full').all():
            child.recurse_compute_push_actions()
        self.compute_push_actions()


@register(Wms.Inventory)
class Line:
    """Represent an assessment for a :class:`Node <Node>` instance.

    This is an inert model, meant to be filled through some user interface.

    If the corresponding :class:`Node` is a leaf, then :attr:`location` could
    be any container under the Node's :attr:`location <Node.location>`.

    But if the :class:`Node` is split, then the :attr:`location` must be
    identical to the Node's :attr:`location <Node.location>`, otherwise
    the simplification of reconciliation :class:`Actions <.action.Action>` can't
    work properly.
    """
    id = Integer(label="Identifier", primary_key=True)
    """Primary key."""
    node = Many2One(model=Wms.Inventory.Node,
                    one2many='lines',
                    nullable=False)
    location = Many2One(model=Wms.PhysObj, nullable=False)
    type = Many2One(model=Wms.PhysObj.Type, nullable=False)
    code = Text()
    properties = Jsonb()
    quantity = Integer(nullable=False)
