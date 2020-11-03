# -*- coding: utf-8 -*-
# This file is a part of the AnyBlok / WMS Base project
#
#    Copyright (C) 2018 Georges Racinet <gracinet@anybox.fr>
#
# This Source Code Form is subject to the terms of the Mozilla Public License,
# v. 2.0. If a copy of the MPL was not distributed with this file,You can
# obtain one at http://mozilla.org/MPL/2.0/.
from anyblok import Declarations
from anyblok.column import Integer
from anyblok.relationship import Many2One

from anyblok_wms_base.exceptions import (
    OperationContainerExpected,
    )

register = Declarations.register
Mixin = Declarations.Mixin
Operation = Declarations.Model.Wms.Operation


@register(Operation)
class Teleportation(Mixin.WmsSingleInputOperation,
                    Mixin.WmsSingleOutcomeOperation,
                    Mixin.WmsInventoryOperation,
                    Operation):
    """Inventory Operation to record unexpected change of location for PhysObj.

    This is similar to Move, but has a distinct functional meaning: the
    change of location is only witnessed after the fact, and it has no
    known explanation.

    Teleportations can exist only in the ``done`` :ref:`state <op_states>`.
    """
    TYPE = 'wms_teleportation'

    id = Integer(label="Identifier",
                 primary_key=True,
                 autoincrement=False,
                 foreign_key=Operation.use('id').options(ondelete='cascade'))
    """Primary key."""

    new_location = Many2One(model='Model.Wms.PhysObj',
                            nullable=False)
    """Where the PhysObj record showed up."""

    @classmethod
    def check_create_conditions(cls, state, dt_execution,
                                new_location=None, **kwargs):
        """Check that new_location is a container.
        """
        if new_location is None or not new_location.is_container():
            raise OperationContainerExpected(
                cls, "new_location field value {offender}",
                offender=new_location)
        super().check_create_conditions(state, dt_execution, **kwargs)

    def after_insert(self):
        """Update :attr:`input` Avatar and create a new one.

        - the state of :attr:`input` is set to ``past``,
        - a new ``present`` Avatar gets created at :attr:`new_location`,
        - care is taken of date & time fields.
        """
        to_move, dt_exec = self.input, self.dt_execution

        to_move.state = 'past'
        self.registry.Wms.PhysObj.Avatar.insert(
            location=self.new_location,
            outcome_of=self,
            state='present',
            dt_from=dt_exec,
            obj=to_move.obj)
