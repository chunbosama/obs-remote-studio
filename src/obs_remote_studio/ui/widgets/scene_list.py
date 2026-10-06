"""「场景」面板（B1/B2/B5/B6），布局对齐 OBS：列表 + 底部一排小按钮。

B5（新建 / 重命名 / 删除）与 B6（拖拽排序）都是编辑类能力，风险高于切换场景，
所以：删除走二次确认、最后一个场景禁止删除、排序碰到老服务端自动降级为只读。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractItemView,
    QInputDialog,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from ...core import protocol as P
from ...core.state_store import CONNECTED, StateStore
from .dock_panel import DockPanel, tool_button, tool_row


class ScenePanel(QWidget):
    def __init__(
        self,
        store: StateStore,
        on_click,
        on_reorder=None,
        on_create=None,
        on_rename=None,
        on_remove=None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.store = store
        self.on_click = on_click
        self.on_reorder = on_reorder
        self.on_create = on_create
        self.on_rename = on_rename
        self.on_remove = on_remove
        self._connected = False
        # 拖拽落位后我们会自己重排列表并重发请求，期间要挡住 refresh 造成的信号回路
        self._suppress = False

        panel = DockPanel("场景")
        self.list_widget = QListWidget()
        self.list_widget.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list_widget.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list_widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list_widget.itemClicked.connect(self._on_item_clicked)
        self.list_widget.itemDoubleClicked.connect(self._on_item_clicked)
        self.list_widget.itemChanged.connect(self._on_item_changed)
        # InternalMove 的落位要等 model 搬完才准，用 model 的 rowsMoved 更可靠
        self.list_widget.model().rowsMoved.connect(self._on_rows_moved)
        self.list_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self._on_context_menu)
        panel.add(self.list_widget, 1)

        self.add_btn = tool_button("+", "新建场景", callback=self._on_add)
        self.remove_btn = tool_button("−", "删除场景", callback=self._on_remove_clicked)
        self.up_btn = tool_button("▲", "上移场景", callback=lambda: self._nudge(-1))
        self.down_btn = tool_button("▼", "下移场景", callback=lambda: self._nudge(1))
        panel.add_layout(
            tool_row(self.add_btn, self.remove_btn, self.up_btn, self.down_btn, align_right=True)
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(panel)

        store.scenes_changed.connect(self.refresh)
        store.current_scene_changed.connect(self.refresh)
        store.preview_scene_changed.connect(self.refresh)
        store.studio_changed.connect(self.refresh)
        store.capabilities_changed.connect(self.refresh)
        store.connection_state_changed.connect(self._on_connection_state)
        self.setEnabled(False)

    # ---------------------------------------------------------------- 状态
    def _on_connection_state(self, state: str) -> None:
        self._connected = state == CONNECTED
        self.setEnabled(self._connected)

    def _scene_edit_supported(self) -> bool:
        return self._connected and self.store.supports(P.REQ_CREATE_SCENE)

    def _reorder_supported(self) -> bool:
        return self._connected and self.store.supports(P.REQ_SET_SCENE_INDEX)

    def refresh(self) -> None:
        if self._suppress:
            return
        current = self.store.current_scene
        preview = self.store.preview_scene if self.store.studio_mode else ""
        self._suppress = True
        try:
            self.list_widget.blockSignals(True)
            self.list_widget.clear()
            selected_row = -1
            for row, scene in enumerate(self.store.scenes):
                is_program = scene.name == current
                is_preview = scene.name == preview
                item = QListWidgetItem(scene.name + ("  （预览）" if is_preview else ""))
                item.setData(Qt.ItemDataRole.UserRole, scene.name)
                if is_program:
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                self.list_widget.addItem(item)
                # 与 OBS 一致：当前操作的那个场景是蓝色选中行
                # （演播室模式下操作的是预览场景，来源列表也跟着它）
                if is_preview or (not preview and is_program):
                    selected_row = row
            if selected_row >= 0:
                self.list_widget.setCurrentRow(selected_row)
            self.list_widget.blockSignals(False)
        finally:
            self._suppress = False
        self._update_buttons()

    def _update_buttons(self) -> None:
        editable = self._scene_edit_supported()
        reorderable = self._reorder_supported()
        self.add_btn.setEnabled(editable)
        self.remove_btn.setEnabled(editable and self._can_remove_current())
        if not editable:
            tip = self.store.support_reason(P.REQ_CREATE_SCENE) or "场景编辑当前不可用"
            self.add_btn.setToolTip(tip)
            self.remove_btn.setToolTip(tip)
        else:
            self.add_btn.setToolTip("新建场景")
            # 删除场景在 v4 上**根本没有对应请求**（v4 无 RemoveScene），
            # 所以这里必须按能力判断，不能只看"能不能编辑"
            if not self.store.supports(P.REQ_REMOVE_SCENE):
                self.remove_btn.setEnabled(False)
                self.remove_btn.setToolTip(
                    self.store.support_reason(P.REQ_REMOVE_SCENE)
                    or "当前服务端不支持删除场景"
                )
            else:
                self.remove_btn.setToolTip(
                    "删除场景" if self._can_remove_current() else "至少要保留一个场景"
                )
        for button in (self.up_btn, self.down_btn):
            button.setEnabled(reorderable)
            button.setToolTip(
                "调整场景顺序" if reorderable
                else (
                    self.store.support_reason(P.REQ_SET_SCENE_INDEX)
                    or "调整场景顺序当前不可用"
                )
            )
        # 不支持排序时连拖拽也禁掉，免得拖了个寂寞
        self.list_widget.setDragEnabled(reorderable)
        self.list_widget.setAcceptDrops(reorderable)

    def _can_remove_current(self) -> bool:
        return len(self.store.scenes) > 1

    def _selected_name(self) -> str:
        item = self.list_widget.currentItem()
        if item is None:
            return self.store.current_scene or ""
        return str(item.data(Qt.ItemDataRole.UserRole) or "")

    # ---------------------------------------------------------------- 交互
    def _on_item_clicked(self, item: QListWidgetItem) -> None:
        if self._suppress:
            return
        name = item.data(Qt.ItemDataRole.UserRole)
        if name:
            self.on_click(name)

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        # 重命名走对话框，这里不接受就地编辑；发现被改了就直接还原
        name = str(item.data(Qt.ItemDataRole.UserRole) or "")
        if name and item.text() != name and item.text() != name + "  （预览）":
            self.refresh()

    def _on_context_menu(self, pos) -> None:
        item = self.list_widget.itemAt(pos)
        if item is None:
            return
        name = str(item.data(Qt.ItemDataRole.UserRole) or "")
        menu = QMenu(self)
        create = QAction("新建场景…", menu)
        create.setEnabled(self._scene_edit_supported())
        create.triggered.connect(self._on_add)
        menu.addAction(create)
        rename = QAction("重命名…", menu)
        rename.setEnabled(self._scene_edit_supported())
        rename.triggered.connect(lambda: self._rename(name))
        menu.addAction(rename)
        remove = QAction("删除", menu)
        remove.setEnabled(self._scene_edit_supported() and self._can_remove_current())
        remove.triggered.connect(lambda: self._remove(name))
        menu.addAction(remove)
        menu.exec(self.list_widget.mapToGlobal(pos))

    # ---------------------------------------------------------------- B5
    def _on_add(self) -> None:
        if not self._scene_edit_supported() or self.on_create is None:
            return
        name, ok = QInputDialog.getText(self, "新建场景", "场景名称：")
        if ok and name.strip():
            self.on_create(name.strip())

    def _rename(self, name: str) -> None:
        if not name or not self._scene_edit_supported() or self.on_rename is None:
            return
        new_name, ok = QInputDialog.getText(self, "重命名场景", "新名称：", text=name)
        if not ok:
            return
        new_name = new_name.strip()
        if new_name and new_name != name:
            self.on_rename(name, new_name)

    def _on_remove_clicked(self) -> None:
        self._remove(self._selected_name())

    def _remove(self, name: str) -> None:
        if not name or not self._scene_edit_supported() or self.on_remove is None:
            return
        if not self._can_remove_current():
            QMessageBox.information(self, "无法删除", "至少要保留一个场景。")
            return
        if name == self.store.current_scene:
            # 删当前正在播的场景影响最大，单独加重提示
            answer = QMessageBox.warning(
                self,
                "删除当前场景",
                f"「{name}」正是当前正在输出的场景，删除会立即中断画面。\n确定要删除吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
        else:
            answer = QMessageBox.question(
                self,
                "删除场景",
                f"确定要删除场景「{name}」吗？此操作不可撤销。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
        if answer == QMessageBox.StandardButton.Yes:
            self.on_remove(name)

    # ---------------------------------------------------------------- B6
    def _on_rows_moved(self, *_args) -> None:
        if self._suppress or self.on_reorder is None:
            return
        if not self._reorder_supported():
            self.refresh()
            return
        # 界面顺序（从上到下）→ OBS 的 sceneIndex（升序，0 在最底层）
        # 界面是倒序显示的，所以行号 r 对应 sceneIndex = len-1-r
        display = [
            str(self.list_widget.item(row).data(Qt.ItemDataRole.UserRole) or "")
            for row in range(self.list_widget.count())
        ]
        display = [name for name in display if name]
        total = len(display)
        if total == 0:
            return
        # 以「选中的那个场景」为锚：它的新行号决定了它该落到哪个 sceneIndex
        anchor = self._selected_name() or display[0]
        self.on_reorder(anchor, total - 1 - display.index(anchor))
        # 本地先按新顺序落状态，避免等服务端回来才动；回执到了再整体刷新校准
        self._suppress = True
        try:
            self.store.reorder_scenes_by_display(display)
        finally:
            self._suppress = False

    def _nudge(self, direction: int) -> None:
        """上移 = 在界面上往上走一行 = sceneIndex 减 1。"""
        name = self._selected_name()
        if not name or not self._reorder_supported() or self.on_reorder is None:
            return
        row = next(
            (r for r in range(self.list_widget.count())
             if self.list_widget.item(r).data(Qt.ItemDataRole.UserRole) == name),
            -1,
        )
        if row < 0:
            return
        target_row = row + direction
        if not 0 <= target_row < self.list_widget.count():
            return
        total = self.list_widget.count()
        self.on_reorder(name, total - 1 - target_row)
