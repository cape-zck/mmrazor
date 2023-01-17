# Copyright (c) OpenMMLab. All rights reserved.
from typing import Dict, List, Optional

import torch
import torch.nn as nn
from mmengine.model import ModuleList
from torch.nn import Module

from mmrazor.models.mutables import BaseMutable, ChannelUnitType
from mmrazor.registry import MODELS
from .base_mutator import MUTABLE_TYPE, BaseMutator
from .group_mixin import GroupMixin


@MODELS.register_module()
class NasMutator(BaseMutator[MUTABLE_TYPE], GroupMixin):
    """The base class for mutable based mutator.

    Args:
        custom_groups (list[list[str]], optional): User-defined search groups.
            All searchable modules that are not in ``custom_group`` will be
            grouped separately.
    """

    def __init__(self,
                 custom_groups: Optional[List[List[str]]] = None,
                 init_cfg: Optional[Dict] = None) -> None:
        super().__init__(init_cfg)

        if custom_groups is None:
            custom_groups = []
        self._custom_groups = custom_groups
        self._search_groups: Dict[str, List[MUTABLE_TYPE]] = None

    def prepare_from_supernet(self, supernet: Module) -> None:
        """Do some necessary preparations with supernet.

        Note:
            For mutable based mutator, we need to build search group first.

        Args:
            supernet (:obj:`torch.nn.Module`): The supernet to be searched
                in your algorithm.
        """
        self._search_groups = dict()

        # prepare for channel mutables
        self.units = self._prepare_from_predefined_model(supernet)
        mutable_units = [unit for unit in self.units if unit.is_mutable]

        _channel_groups = dict()
        for id, unit in enumerate(ModuleList(mutable_units)):
            _channel_groups['channel' + '_' + str(id)] = [unit]
        self._search_groups.update(_channel_groups)

        # prepare for value mutables
        _value_groups = self.build_search_groups(supernet, self._custom_groups)
        self._search_groups.update(_value_groups)

    def prepare_arch_params(self):
        """This function will build searchable params for each layer, which are
        generally used in differentiable search algorithms, such as Darts'
        series. Each name corresponds to an search param, so the Mutables with
        the same name share the same search param.

        Returns:
            torch.nn.ParameterDict: search params got by the search_space.
        """
        self._arch_params = nn.ParameterDict()

        for name, mutables in self.search_groups.items():
            self._arch_params[name] = nn.Parameter(
                torch.randn(mutables[0].num_choices) * 1e-3)

        self._modify_supernet_forward()

    @property
    def search_groups(self) -> Dict[str, List[MUTABLE_TYPE]]:
        """Search group of supernet.

        Note:
            For mutable based mutator, the search group is composed of
            corresponding mutables.

        Raises:
            RuntimeError: Called before search group has been built.

        Returns:
            Dict[int, List[MUTABLE_TYPE]]: Search group.
        """
        if self._search_groups is None:
            raise RuntimeError(
                'Call `prepare_from_supernet` first to get the search space.')
        return self._search_groups

    @property
    def arch_params(self) -> Dict[str, List[MUTABLE_TYPE]]:
        """Search params of supernet.

        Note:
            For mutable based mutator, the search group is composed of
            corresponding mutables.

        Raises:
            RuntimeError: Called before search group has been built.

        Returns:
            Dict[int, List[MUTABLE_TYPE]]: Search group.
        """
        if self._arch_params is None:
            raise RuntimeError(
                'Call `prepare_arch_params` first to get the search params.')
        return self._arch_params

    def _prepare_from_predefined_model(self, model: Module):
        """Initialize units using the model with pre-defined dynamic-ops and
        mutable-channels."""
        from mmrazor.models.mutables import OneShotMutableChannelUnit

        self._name2unit: Dict[str, ChannelUnitType] = {}
        units = OneShotMutableChannelUnit.init_from_predefined_model(model)

        for unit in units:
            unit.current_choice = unit.max_choice
            self._name2unit[unit.name] = unit

        return units

    def _modify_supernet_forward(self):
        """Modify the DiffMutableModule's default arch_param in forward.

        In MMRazor, the `DiffMutableModule` needs `arch_param` in the forward.
        Here we use partial function to assign the corresponding `arch_param`
        to each `DiffMutableModule`.
        """
        for name, mutables in self.search_groups.items():
            for mutable in mutables:
                mutable.set_forward_args(arch_param=self.arch_params[name])

    # choice manage

    def sample_choices(self, kind='random') -> Dict:
        """Random sample choices by search space."""
        choices = dict()
        for name, mutables in self.search_groups.items():
            if hasattr(self, 'arch_params'):
                arch_param = self.arch_params[name]
                choices[name] = mutables[0].sample_choice(arch_param)
            else:
                if kind == 'max':
                    choices[name] = mutables[0].max_choice
                elif kind == 'min':
                    choices[name] = mutables[0].min_choice
                else:
                    choices[name] = mutables[0].sample_choice()
        return choices

    def set_choices(self, choices: Dict) -> None:
        """Set choices for each mutable in search space."""
        for name, mutables in self.search_groups.items():
            if name not in choices:
                if mutables[0].alias not in choices:
                    # allow optional target_prune_ratio
                    continue
                else:
                    choice = choices[mutables[0].alias]
            else:
                choice = choices[name]

            for mutable in mutables:
                mutable.current_choice = choice

    def fix_choices(self):
        """Fix choices."""
        self.set_choices(self.sample_choices())
        for module in self.architecture.modules():
            if isinstance(module, BaseMutable):
                if not module.is_fixed:
                    module.fix_chosen(module.current_choice)
        self.is_supernet = False

    @property
    def max_choices(self) -> Dict:
        """Get max choices for each mutable in search space."""
        max_choices = dict()
        for name, mutables in self.search_groups.items():
            max_choices[name] = mutables[0].max_choice

        return max_choices

    @property
    def min_choices(self) -> Dict:
        """Get min choices for each mutable in search space."""
        min_choices = dict()
        for name, mutables in self.search_groups.items():
            min_choices[name] = mutables[0].min_choice

        return min_choices

    @property
    def current_choices(self) -> Dict:
        """Get current choices by search space."""
        current_choices = dict()
        for name, mutables in self.search_groups.items():
            current_choices[name] = mutables[0].current_choice

        return current_choices

    def set_max_choices(self):
        """Set max choices for each mutable in search space."""
        for name, mutables in self.search_groups.items():
            choice = self.max_choices[name]
            for mutable in mutables:
                mutable.current_choice = choice

    def set_min_choices(self):
        """Set min choices for each mutable in search space."""
        for name, mutables in self.search_groups.items():
            choice = self.min_choices[name]
            for mutable in mutables:
                mutable.current_choice = choice
