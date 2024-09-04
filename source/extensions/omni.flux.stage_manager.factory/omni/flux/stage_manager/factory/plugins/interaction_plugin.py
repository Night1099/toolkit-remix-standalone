"""
* SPDX-FileCopyrightText: Copyright (c) 2024 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
* SPDX-License-Identifier: Apache-2.0
*
* Licensed under the Apache License, Version 2.0 (the "License");
* you may not use this file except in compliance with the License.
* You may obtain a copy of the License at
*
* https://www.apache.org/licenses/LICENSE-2.0
*
* Unless required by applicable law or agreed to in writing, software
* distributed under the License is distributed on an "AS IS" BASIS,
* WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
* See the License for the specific language governing permissions and
* limitations under the License.
"""

import abc
from asyncio import Future, ensure_future
from functools import partial
from typing import TYPE_CHECKING, Any, Iterable

import carb
import omni.kit.app
from omni import ui
from omni.flux.stage_manager.factory import StageManagerDataTypes as _StageManagerDataTypes
from pydantic import Field, PrivateAttr, root_validator, validator

from .base import StageManagerUIPluginBase as _StageManagerUIPluginBase
from .column_plugin import LengthUnit as _LengthUnit
from .column_plugin import StageManagerColumnPlugin as _StageManagerColumnPlugin
from .context_plugin import StageManagerContextPlugin as _StageManagerContextPlugin
from .filter_plugin import StageManagerFilterPlugin as _StageManagerFilterPlugin
from .tree_plugin import StageManagerTreePlugin as _StageManagerTreePlugin

if TYPE_CHECKING:
    from .column_plugin import ColumnWidth as _ColumnWidth


class StageManagerInteractionPlugin(_StageManagerUIPluginBase, abc.ABC):
    """
    A plugin that encompasses other plugins in the Stage Manager.
    The Interaction Plugin builds the TreeWidget and uses the filter, column & widget plugins to build the contents.
    """

    tree: _StageManagerTreePlugin = Field(..., description="The tree plugin defining the model and delegate")
    filters: list[_StageManagerFilterPlugin] = Field(..., description="Filters to apply to the context data")
    columns: list[_StageManagerColumnPlugin] = Field(..., description="Columns to display in the TreeWidget")

    required_filters: list[_StageManagerFilterPlugin] = Field(
        [],
        description="Mandatory filters defined by the interaction plugin. Will never be displayed in UI.",
        exclude=True,
    )

    tree_widget: ui.TreeView | None = Field(
        None, description="The TreeView widget used to display the data", exclude=True
    )

    # The context will be set by the core. This will be used to get the context data.
    _context: _StageManagerContextPlugin | None = PrivateAttr()
    _result_frames: list[ui.Frame] = PrivateAttr()
    _item_expansion_states: dict[int, bool] = PrivateAttr()
    _filter_items_changed_subs: list[carb.Subscription] = PrivateAttr()
    _item_expanded_sub: carb.Subscription = PrivateAttr()
    _update_expansion_task: Future | None = PrivateAttr()
    _is_initialized: bool = PrivateAttr()

    _FILTERS_HORIZONTAL_PADDING: int = 24
    _FILTERS_VERTICAL_PADDING: int = 8
    _RESULTS_HORIZONTAL_PADDING: int = 16
    _RESULTS_VERTICAL_PADDING: int = 4

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        self._context = None

        self._result_frames = []
        self._item_expansion_states = {}

        self._filter_items_changed_subs = []
        self._item_expanded_sub = None

        self._update_expansion_task = None

        self._is_initialized = False

    @validator("filters", "required_filters", allow_reuse=True)
    def check_unique_filters(cls, v):  # noqa N805
        # Use a list + validator to keep the list order
        return list(dict.fromkeys(v))

    @root_validator(allow_reuse=True)
    def check_plugin_compatibility(cls, values):  # noqa N805
        # In the root validator, plugins are already resolved
        cls._check_plugin_compatibility(values.get("tree").name, values.get("compatible_trees"))
        for filter_plugin in values.get("filters"):
            cls._check_plugin_compatibility(filter_plugin.name, values.get("compatible_filters"))
        for filter_plugin in values.get("required_filters"):
            # The interaction filters are not instantiated at this point
            cls._check_plugin_compatibility(filter_plugin.get("name"), values.get("compatible_filters"))
        for column_plugin in values.get("columns"):
            for widget_plugin in column_plugin.widgets:
                cls._check_plugin_compatibility(widget_plugin.name, values.get("compatible_widgets"))

        return values

    @classmethod
    @property
    def compatible_data_type(cls) -> _StageManagerDataTypes:
        """
        The data type this plugin supports
        """
        return _StageManagerDataTypes.GENERIC

    @classmethod
    @property
    @abc.abstractmethod
    def compatible_trees(cls) -> list[str]:
        """
        Get the list of tree plugins compatible with this interaction plugin
        """
        pass

    @classmethod
    @property
    @abc.abstractmethod
    def compatible_filters(cls) -> list[str]:
        """
        Get the list of filter plugins compatible with this interaction plugin
        """
        pass

    @classmethod
    @property
    @abc.abstractmethod
    def compatible_widgets(cls) -> list[str]:
        """
        Get the list of widget plugins compatible with this interaction plugin
        """
        pass

    @classmethod
    def _check_plugin_compatibility(cls, value: Any, compatible_items: list[Any] | None) -> Any:
        """
        Check if the given value is contained within the compatible items.

        Args:
            value: The value to be checked.
            compatible_items: A list of compatible items to compare value to

        Raises:
            ValueError: If the value is not compatible with any of the compatible items.

        Returns:
            The value if it is compatible
        """
        if value not in (compatible_items or []):
            raise ValueError(
                f"The selected plugin is not compatible with this plugin -> {value}\n"
                f"  Compatible plugin(s): {', '.join(compatible_items)}"
            )
        return value

    def setup(self, value: _StageManagerContextPlugin):
        """
        Set up the interaction plugin with the given context.

        Args:
            value: the context the interaction plugin should use
        """

        # Subscribe to the on_filter_items_changed event to refresh the tree widget model
        self.tree.model.clear_filter_functions()
        for filter_plugin in self.filters + self.required_filters:
            if not filter_plugin.enabled:
                continue
            self._filter_items_changed_subs.append(
                filter_plugin.subscribe_filter_items_changed(self._on_filter_items_changed)
            )
            # Some models might need to filter children items so pass the filter functions down
            self.tree.model.add_filter_functions([filter_plugin.filter_items])

        enabled_columns = [c for c in self.columns if c.enabled]

        self.tree.model.column_count = len(enabled_columns)
        self.tree.delegate.set_column_builders(enabled_columns)
        self._item_expanded_sub = self.tree.delegate.subscribe_item_expanded(self._on_item_expanded)

        self._context = value
        self._validate_data_type()
        self._update_context_items()

        self._is_initialized = True

    def build_ui(self):  # noqa PLW0221
        """
        The method used to build the UI for the plugin.

        Raises:
            ValueError: If the plugin was not initialized before building the UI.
        """

        if not self.enabled:
            return

        if not self._is_initialized:
            raise ValueError("InteractionPlugin.setup() must be called before build_ui()")

        self._result_frames.clear()

        enabled_filters = [f for f in self.filters if f.enabled]
        enabled_columns = [c for c in self.columns if c.enabled]

        column_widths = [self._get_ui_length(c.width) for c in enabled_columns]

        with ui.VStack():
            # Filters UI
            ui.Spacer(height=ui.Pixel(self._FILTERS_VERTICAL_PADDING), width=0)
            with ui.HStack(height=0):
                ui.Spacer(height=0)
                with ui.HStack(width=0, height=0, spacing=self._FILTERS_HORIZONTAL_PADDING):
                    for filter_plugin in enabled_filters:
                        # Some filters should not be displayed in the UI
                        if not filter_plugin.display:
                            continue
                        ui.Frame(tooltip=filter_plugin.tooltip, build_fn=filter_plugin.build_ui)
                ui.Spacer(width=12, height=0)
            ui.Spacer(height=ui.Pixel(self._FILTERS_VERTICAL_PADDING), width=0)

            with ui.ZStack():
                with ui.VStack():
                    # Tree UI
                    with ui.ScrollingFrame(name="TreePanelBackground"):
                        self.tree_widget = ui.TreeView(
                            self.tree.model,
                            delegate=self.tree.delegate,
                            root_visible=False,
                            header_visible=True,
                            columns_resizable=False,  # There's no way to resize the results after resizing a column
                            column_widths=column_widths,
                        )

                    # Results UI
                    with ui.ZStack(height=0):
                        ui.Rectangle(name="TabBackground")
                        with ui.VStack():
                            ui.Spacer(height=self._RESULTS_VERTICAL_PADDING, width=0)
                            with ui.HStack():
                                for index, column in enumerate(enabled_columns):
                                    self._result_frames.append(
                                        ui.Frame(
                                            width=column_widths[index],
                                            horizontal_clipping=True,
                                            build_fn=partial(column.build_result_ui, self.tree.model),
                                        )
                                    )
                                # Spacing for the scrollbar
                                ui.Spacer(width=self._RESULTS_HORIZONTAL_PADDING, height=0)
                            ui.Spacer(height=self._RESULTS_VERTICAL_PADDING, width=0)

                # Column separators
                with ui.HStack():
                    for index, _ in enumerate(enabled_columns):
                        with ui.HStack(width=column_widths[index]):
                            ui.Spacer(height=0)
                            # Don't draw a line at the end of the tree
                            if index < len(enabled_columns) - 1:
                                ui.Rectangle(width=ui.Pixel(2), name="WizardSeparator")
                    # Spacing for the scrollbar
                    ui.Spacer(width=self._RESULTS_HORIZONTAL_PADDING, height=0)

    @staticmethod
    def _get_ui_length(column_width: "_ColumnWidth") -> ui.Length:
        """
        Args:
            column_width: A ColumnWidth base model describing the length object

        Returns:
            An OmniUI length object
        """
        length_class = ui.Fraction
        match column_width.unit:
            case _LengthUnit.FRACTION:
                length_class = ui.Fraction
            case _LengthUnit.PERCENT:
                length_class = ui.Percent
            case _LengthUnit.PIXEL:
                length_class = ui.Pixel
        return length_class(column_width.value)

    def _on_filter_items_changed(self):
        """
        Event handler for to execute when event widgets are updated
        """
        self._update_context_items()

        # Rebuild the result UI with the update model data
        for frame in self._result_frames:
            frame.rebuild()

        self._update_expansion_states()

    def _on_item_expanded(self, item: ui.AbstractItem, expanded: bool):
        self._item_expansion_states[hash(item)] = expanded

    def _validate_data_type(self):
        """
        Validate the compatibility of the context data type

        Raises:
            ValueError: If the data type is not compatible with this plugin
        """
        if self._context.data_type != self.compatible_data_type:
            raise ValueError(
                f"The context plugin data type is not compatible with this interaction plugin -> {self.name} -> "
                f"{self._context.data_type.value} != {self.compatible_data_type.value}"
            )

    def _update_context_items(self):
        """
        Set the context items for the interaction plugin Tree model and refresh the model.
        """
        self.tree.model.context_items = self._filter_context_items(self._context.get_items())
        self.tree.model.refresh()

    def _filter_context_items(self, items: Iterable[Any]) -> list[Any]:
        """
        Filter the context items based on the enabled filters.

        Args:
            items: The items to be filtered

        Returns:
            A filtered list of items
        """
        filtered_items = list(items)
        for filter_plugin in self.filters + self.required_filters:
            if filter_plugin.enabled:
                filtered_items = filter_plugin.filter_items(filtered_items)
        return filtered_items

    def _update_expansion_states(self):
        """
        Fire and forget the `_update_expansion_states_deferred` function
        """
        if self._update_expansion_task:
            self._update_expansion_task.cancel()
        self._update_expansion_task = ensure_future(self._update_expansion_states_deferred())

    async def _update_expansion_states_deferred(self):
        """
        Wait 1 frame, then update the expansion state of the Tree items based on their cached state
        """
        await omni.kit.app.get_app().next_update_async()

        items_dict = self.tree.model.items_dict
        for item_hash, expanded in self._item_expansion_states.items():
            item = items_dict.get(item_hash)
            if not item:
                continue
            self.tree_widget.set_expanded(item, expanded, False)

    def destroy(self):
        if self._update_expansion_task:
            self._update_expansion_task.cancel()
            self._update_expansion_task = None

    class Config(_StageManagerUIPluginBase.Config):
        fields = {
            **_StageManagerUIPluginBase.Config.fields,
            "compatible_trees": {"exclude": True},
            "compatible_filters": {"exclude": True},
            "compatible_widgets": {"exclude": True},
        }
