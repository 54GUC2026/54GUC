"""One SIM/network form: accepted observations, a local draft, and scoped results."""
from collections.abc import Mapping
from time import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox, QFormLayout, QGroupBox, QLabel, QLayout, QLineEdit, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from arch4.foundation.network_country_reference import MCC_NAMES, country_verdict
from arch4.foundation.network_intention import resolve_network_choice
from arch4.foundation.network_settings import validate_connect_network_draft, connect_network_input_message
from arch4.core_contracts.network_settings import subscription_key
from arch4.protected_credentials import seal_apn_credentials
from arch4.ngui.front_register import PresentationState
from arch4.ngui.login_lifecycle import login_lifecycle_state
from .interactions import bind_button, bind_combo_changed, bind_text_draft, stamp_draft_control
from arch4.ngui.interactions import IntentKind
from .network_feedback import network_diagnostic_text, network_failure_text, network_progress_text


class NetworkSettingsEditor(QGroupBox):
    def __init__(self, register):
        super().__init__("SIM && Network")
        self.prefix = "nguiDeviceNetwork"
        self._truth = self._observation = None
        self._bound_subscription = ""
        self._bound_apn = self._bound_selection = self._bound_roaming = None
        self._filling = self._submitting = False
        self._state = "draft"
        self._dirty = set()
        self._saved_profile_id = ""
        self._profile_scope = ("", "")
        self._library = {}
        self._library_signature = None
        self._apn_detached = False
        self._new_apn = False
        self._modem_values = {}
        self._connection_context = None
        self._remembered_preset_context = None
        self._submitted_form = None
        self._writable = True
        self._profile_creation = False
        self._applying = False
        self._result_since = time()
        self._result_action_id = ""
        layout = QVBoxLayout(self)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        layout.setSpacing(8)
        self.inspection_status = QLabel("Leave the fields unchanged to use the router's settings, then click Connect. To request changes, complete the fields first.")
        self.inspection_status.setWordWrap(True)
        layout.addWidget(self.inspection_status)
        self.details = QWidget()
        form = QFormLayout(self.details)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(6)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        countries = [("Unknown", "")] + sorted({(names[0], names[0]) for names in MCC_NAMES.values()})
        self.sim_country = self._combo("SimCountry", countries)
        self.roaming = self._combo("Roaming", [("Disabled", False), ("Enabled", True), ("Unknown", None)])
        self.network = self._combo("Network", [])
        self.network.setEditable(True)
        self.network.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.network.lineEdit().setMaxLength(128)
        self.network.lineEdit().setPlaceholderText("Network name and code, or 5–6 digit code")
        self.visited_country = self._combo("VisitedCountry", countries)
        self.selection = self._combo("Selection", [("AUTO", "auto"), ("Specified", "manual"), ("Unknown", "")])
        for label, widget in (("SIM country", self.sim_country), ("Roaming", self.roaming),
                              ("Network country", self.visited_country), ("Network", self.network),
                              ("Network selection", self.selection)):
            form.addRow(label, widget)
        self.apn_fields = QGroupBox("APN settings")
        apn_form = QFormLayout(self.apn_fields)
        apn_form.setSpacing(6)
        apn_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.apn_name = self._combo("ApnName", [])
        self.apn_name.setEditable(True)
        self.apn_name.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.apn_name.lineEdit().setMaxLength(128)
        self.apn_name.lineEdit().setPlaceholderText("Enter a profile name")
        self.apn_value = self._text("ApnValue")
        self.apn_user = self._text("ApnUser")
        self.apn_password = self._text("ApnPassword")
        self.apn_password.setEchoMode(QLineEdit.Password)
        self.authentication = self._combo("Authentication", [("Unknown", ""), ("None", "none"), ("PAP", "pap"), ("CHAP", "chap"), ("PAP / CHAP", "auto")])
        self.protocol = self._combo("Protocol", [("Unknown", ""), ("IPv4", "ipv4"), ("IPv6", "ipv6"), ("IPv4 / IPv6", "ipv4v6")])
        for label, widget in (("Profile name", self.apn_name), ("APN", self.apn_value), ("Username", self.apn_user),
                              ("Password", self.apn_password), ("Authentication", self.authentication), ("IP protocol", self.protocol)):
            apn_form.addRow(label, widget)
        form.addRow(self.apn_fields)
        layout.addWidget(self.details)
        self.apply = QPushButton("Apply settings")
        self.apply.setObjectName(self.prefix + "Apply")
        bind_button(self.apply, register, IntentKind.APPLY_NETWORK_SETTINGS, self._apply_payload)
        layout.addWidget(self.apply)
        self.feedback = QLabel()
        self.feedback.setObjectName(self.prefix + "Feedback")
        self.feedback.setTextFormat(Qt.TextFormat.PlainText)
        self.feedback.setWordWrap(True)
        self.feedback.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.feedback)
        self.diagnostic = QLabel()
        self.diagnostic.setObjectName(self.prefix + "Diagnostic")
        self.diagnostic.setTextFormat(Qt.TextFormat.PlainText)
        self.diagnostic.setWordWrap(True)
        self.diagnostic.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.diagnostic)
        layout.addStretch(1)
        for key, widget in (("sim_country", self.sim_country), ("roaming", self.roaming), ("country", self.visited_country),
                            ("selection", self.selection), ("authentication", self.authentication), ("ip_protocol", self.protocol)):
            bind_combo_changed(widget, lambda key=key: self._edited(key))
        bind_combo_changed(self.network, self._network_changed)
        bind_text_draft(self.network.lineEdit(), lambda _text: self._network_changed())
        self.network.lineEdit().editingFinished.connect(self._normalize_network_choice)
        for widget in (self.network.lineEdit(), self.apn_name.lineEdit(), self.apn_value,
                       self.apn_user, self.apn_password):
            widget.editingFinished.connect(self._validate_disconnected_fields)
        bind_combo_changed(self.apn_name, self._profile_selected)
        bind_text_draft(self.apn_name.lineEdit(), lambda _text: self._edited("name"))
        for key, widget in (("apn", self.apn_value), ("username", self.apn_user), ("password", self.apn_password)):
            bind_text_draft(widget, lambda _text, key=key: self._edited(key))
        self._visibility()

    def _combo(self, name, entries):
        widget = QComboBox()
        widget.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        widget.setMinimumHeight(28)
        widget.setObjectName(self.prefix + name)
        for label, data in entries:
            widget.addItem(label, data)
        stamp_draft_control(widget)
        return widget

    def _text(self, name):
        widget = QLineEdit()
        widget.setMinimumHeight(28)
        widget.setObjectName(self.prefix + name)
        widget.setMaxLength(128)
        stamp_draft_control(widget)
        return widget

    def _field(self, name):
        return self._modem_values.get(name)

    def _sim(self):
        value = self._field("sim.network")
        return value if isinstance(value, Mapping) else {}

    def set_connection_context(self, context):
        # A last reading can prefill the same router, never a different login target.
        if (self._connection_context is not None
                and all(self._connection_context)
                and context != self._connection_context):
            self._modem_values.clear()
            self._observation = None
            self._bound_subscription = ""
            self._bound_apn = self._bound_selection = self._bound_roaming = None
            self._discard_draft()
            self._submitted_form = None
            self._fill_settings(None)
            self._remembered_preset_context = None
        self._connection_context = context

    def apply_remembered_preset(self, preset):
        """Show a previous choice before Connect; the current SIM is checked later."""
        if (not self._writable or not isinstance(preset, Mapping) or not preset
                or self._connection_context is None
                or self._remembered_preset_context == self._connection_context):
            return
        self._remembered_preset_context = self._connection_context
        # Remembered defaults initialize a form; a late publication must never
        # replace user edits or the request already submitted with Connect.
        if (self._dirty or self._submitted_form is not None
                or self._state not in {"draft", "stopped", "failed", ""}):
            return
        self._filling = True
        self._set_combo(self.sim_country, preset.get("sim_country") or "")
        self._set_combo(self.roaming, preset.get("roaming_enabled"))
        self.network.setEditText(str(preset.get("network_plmn") or ""))
        self._set_combo(self.visited_country, preset.get("network_country") or "")
        self._set_combo(self.selection, preset.get("selection") or "")
        self._filling = False
        # Connect approves the displayed preset; another edit is not required.
        self._dirty.update({"roaming", "network", "selection"})
        self._bound_subscription = str(preset.get("subscription_key") or "")
        self._sync_profile_scope()
        # An observed profile with no saved secret is a public form, not a
        # request to resolve a password that the library never received.
        self._saved_profile_id = str(preset.get("saved_profile_id") or "") if preset.get('password_known') else ''
        self._apn_detached = bool(self._saved_profile_id)
        self._set_apn(preset.get("apn") or {})
        if preset.get("apn"):
            self._dirty.update({"name", "apn", "username", "authentication", "ip_protocol"})
        if self._saved_profile_id:
            self.apn_password.setPlaceholderText(
                "Saved password" if preset.get("password_known") else "Password not saved — enter it"
            )
        self._visibility()

    def set_write_permission(self, writable):
        """Consume the selected profile's permission; never infer it from a vendor."""
        if type(writable) is not bool:
            raise TypeError("network_settings_write_permission_bool_required")
        if self._writable and not writable:
            self._discard_draft()
            self._submitted_form = None
            self.feedback.clear()
            self._fill_settings(self._observation)
        self._writable = writable
        self._visibility()

    def set_profile_creation(self, allowed):
        if type(allowed) is not bool:
            raise TypeError("apn_profile_creation_bool_required")
        self._profile_creation = allowed

    def _mobile_off(self):
        field = self._truth.fields.get("device.mobile_data_enabled") if self._truth else None
        return bool(field and field.state is PresentationState.VALID and field.value is False)

    def _editable(self):
        return self._writable and not self._applying and (
            self._state in {"draft", "stopped", "failed", ""}
            or self._state == "connected" and self._mobile_off())

    def _apply_payload(self):
        if self._state != "connected" or not self._editable() or self._truth.matrix_identity is None:
            raise ValueError("connected_mobile_off_required")
        draft = self.draft()
        payload = {"network_draft": draft, "matrix_identity": self._truth.matrix_identity.as_dict()}
        self._submitted_form = self._capture_form()
        self._result_since = time()
        self._result_action_id = ""
        self._applying = True
        self.feedback.setText("Applying settings…")
        self._visibility()
        return payload

    def _discard_draft(self):
        self._dirty.clear()
        self._saved_profile_id = ""
        self._apn_detached = self._new_apn = False
        self._library_signature = None
        self._filling = True
        self.apn_password.clear()
        self._filling = False

    def start_connection(self):
        self._submitted_form = self._capture_form() if self._has_changes() else None
        self._state = "connecting"
        self._result_since = time()
        self._result_action_id = ""
        self.feedback.setText("Checking the active SIM before applying settings…" if self._has_changes() else "")
        self._visibility()

    def _capture_form(self):
        """Keep the unsaved Connect form while the connected view shows modem truth."""
        return {
            "sim_country": self.sim_country.currentData(),
            "roaming": self.roaming.currentData(),
            "network": self.network.currentText(),
            "country": self.visited_country.currentData(),
            "selection": self.selection.currentData(),
            "name": self.apn_name.currentText(),
            "apn": self.apn_value.text(),
            "username": self.apn_user.text(),
            "password": self.apn_password.text(),
            "authentication": self.authentication.currentData(),
            "ip_protocol": self.protocol.currentData(),
            "dirty": set(self._dirty),
            "subscription": self._bound_subscription,
            "saved_profile_id": self._saved_profile_id,
            "apn_detached": self._apn_detached,
            "new_apn": self._new_apn,
        }

    def _restore_submitted_form(self):
        saved = self._submitted_form
        self._submitted_form = None
        if saved is not None:
            self._bound_subscription = saved["subscription"]
        self._show_submitted_form(saved)

    def _show_submitted_form(self, saved):
        if saved is None:
            return
        restore_all = self._observation is None
        self._filling = True
        try:
            for key, widget in (("sim_country", self.sim_country), ("roaming", self.roaming),
                                ("country", self.visited_country), ("selection", self.selection),
                                ("authentication", self.authentication), ("ip_protocol", self.protocol)):
                if restore_all or key in saved["dirty"] or (key in {"selection", "country"} and "network" in saved["dirty"]):
                    self._set_combo(widget, saved[key])
            if restore_all or "network" in saved["dirty"]:
                self.network.setEditText(saved["network"])
            if restore_all or "name" in saved["dirty"]:
                self.apn_name.setCurrentIndex(-1)
                self.apn_name.setEditText(saved["name"])
            for key, widget in (("apn", self.apn_value), ("username", self.apn_user),
                                ("password", self.apn_password)):
                if restore_all or key in saved["dirty"]:
                    widget.setText(saved[key])
        finally:
            self._filling = False
        self._dirty = set(saved["dirty"])
        self._saved_profile_id = saved["saved_profile_id"]
        self._apn_detached = saved["apn_detached"]
        self._new_apn = saved["new_apn"]
        self._sync_profile_scope()
        self._library_signature = None
        observed_apn = (self._observation or {}).get("apn") or {}
        if (saved["name"], saved["apn"]) != (observed_apn.get("name", ""), observed_apn.get("apn", "")):
            self.apn_password.setPlaceholderText("")

    def _edited(self, key):
        if self._filling:
            return
        self._dirty.add(key)
        if not self._submitting:
            self.feedback.clear()
        if key in {"sim_country", "roaming"} and self.roaming.currentData() is False:
            self._filling = True
            self._set_combo(self.visited_country, self.sim_country.currentData() or "")
            self._filling = False
            self._dirty.discard("country")
        self._visibility()
        if key in {"sim_country", "country", "roaming"} and self.network.currentText().strip():
            self._network_changed(explicit=False)

    def _network_choice(self):
        choices = [{"plmn": str(self.network.itemData(i) or ""),
                    "name": self.network.itemText(i).rsplit(" (", 1)[0]
                    if " (" in self.network.itemText(i) else ""}
                   for i in range(self.network.count())]
        country = self.visited_country.currentData() or (
            self.sim_country.currentData() if self.roaming.currentData() is False else "")
        return resolve_network_choice(self.network.currentText(), choices, country=country or "")

    def network_code(self):
        return self._network_choice()[0]

    def _normalize_network_choice(self):
        if self._filling or "network" not in self._dirty or not self._editable():
            return
        code, label, error = self._network_choice()
        if error:
            self.feedback.setText(error)
        elif code:
            if (self._bound_subscription, code) != self._profile_scope:
                self._network_changed(explicit=False)
            self._filling = True
            self.network.setEditText(label)
            self._filling = False

    def _network_changed(self, *, explicit=True):
        if self._filling:
            return
        self._edited("network")
        if explicit and "selection" not in self._dirty:
            self._filling = True
            self._set_combo(self.selection, "manual")
            self._filling = False
        code, _label, error = self._network_choice()
        if error or not code:
            self.feedback.setText(error)
            return
        if self._sync_profile_scope(detach_changed_network=True):
            self._refresh_profiles()
        if "country" not in self._dirty and self.roaming.currentData() is True:
            _, country = country_verdict("", self.network_code())
            self._filling = True
            self._set_combo(self.visited_country, country)
            self._filling = False
        self._visibility()

    def _sync_profile_scope(self, *, detach_changed_network=False):
        """Only valid choices change local APN scope; invalid draft text stays editable."""
        code, _label, error = self._network_choice()
        if error or not code:
            if (self._profile_scope[0] != self._bound_subscription
                    and (self._bound_subscription or self._submitted_form is None)):
                self._profile_scope = (self._bound_subscription, "")
                self._library_signature = None
            return False
        scope = (self._bound_subscription, code)
        if scope == self._profile_scope:
            return False
        changed_network = bool(self._profile_scope[1] and self._profile_scope[1] != code)
        self._profile_scope = scope
        if detach_changed_network and changed_network:
            self._saved_profile_id = ""
            self._apn_detached = True
            self._dirty.difference_update({"name", "apn", "username", "password", "authentication", "ip_protocol"})
            self._set_apn({})
        self._library_signature = None
        return True

    @staticmethod
    def _set_combo(widget, value):
        widget.setCurrentIndex(max(0, widget.findData(value)))

    def _set_apn(self, apn, *, untouched=False):
        self._filling = True
        for key, widget in (("name", self.apn_name), ("apn", self.apn_value), ("username", self.apn_user)):
            if not untouched or key not in self._dirty:
                if key == "name":
                    widget.setCurrentIndex(-1)
                    widget.setEditText(apn.get(key, ""))
                else:
                    widget.setText(apn.get(key, ""))
        for key, widget in (("authentication", self.authentication), ("ip_protocol", self.protocol)):
            if not untouched or key not in self._dirty:
                self._set_combo(widget, apn.get(key) or "")
        if not untouched or "password" not in self._dirty:
            self.apn_password.clear()
            self.apn_password.setPlaceholderText("Saved in modem — leave unchanged" if apn else "")
        self._filling = False

    def _fill_settings(self, obs):
        obs = obs or {}
        sim = self._sim()
        self._filling = True
        if "sim_country" not in self._dirty:
            self._set_combo(self.sim_country, MCC_NAMES.get(sim.get("mcc", ""), ("",))[0])
        if "roaming" not in self._dirty:
            self._set_combo(self.roaming, obs.get("roaming_enabled"))
        if "selection" not in self._dirty:
            self._set_combo(self.selection, obs.get("selection") or "")
        if "network" not in self._dirty:
            code = (obs.get("target_plmn") if obs.get("selection") == "manual" else "") or sim.get("home_plmn", "")
            name = sim.get("home_provider", "") if code == sim.get("home_plmn") else ""
            serving = str(self._field("provider.plmn") or "")
            if code and code == serving:
                name = str(self._field("provider.name") or name)
            self.network.setEditText(f"{name} ({code})" if name and code else code)
            self.network.setToolTip("" if code else "The modem has not identified the SIM home network. Select or enter its network code.")
        if "country" not in self._dirty:
            _, country = country_verdict("", self.network_code())
            self._set_combo(self.visited_country, country)
        self._filling = False
        if not self._apn_detached and not self._saved_profile_id:
            self._set_apn(obs.get("apn") or {}, untouched=True)
        if self._submitted_form is None:
            self._sync_profile_scope()
        self._refresh_profiles()

    def _refresh_profiles(self):
        field = self._truth.fields.get("network.apn_profiles") if self._truth else None
        library = field.value if field and isinstance(field.value, Mapping) else {}
        rows = [dict(row) for row in library.get("profiles", ())
                if row.get("subscription_key") == self._profile_scope[0] and row.get("network_plmn") == self._profile_scope[1]]
        obs = self._observation or {}
        current_network = (obs.get("target_plmn") if obs.get("selection") == "manual" else "") or self._sim().get("home_plmn", "")
        current = (obs.get("apn") or {}) if self._profile_scope == (self._bound_subscription, current_network) else {}
        signature = (self._profile_scope, repr(current), repr(rows), repr(library.get("networks", ())))
        self._library = {row["profile_id"]: row for row in rows}
        if signature == self._library_signature:
            return
        self._library_signature = signature
        self._filling = True
        name, network = self.apn_name.currentText(), self.network.currentText()
        self.apn_name.clear()
        if current.get("name"):
            self.apn_name.addItem(current["name"], "@current")
        for row in rows:
            self.apn_name.addItem(row["name"], row["profile_id"])
        self.apn_name.addItem("NEW", "@new")
        self.apn_name.setCurrentIndex(-1)
        self.apn_name.setEditText(name)
        choices = {str(row.get("plmn", "")): str(row.get("name", "")) for row in library.get("networks", ())}
        sim = self._sim()
        if sim.get("home_plmn"):
            choices.setdefault(sim["home_plmn"], sim.get("home_provider", ""))
        serving = str(self._field("provider.plmn") or "")
        if serving:
            choices.setdefault(serving, str(self._field("provider.name") or ""))
        for row in rows:
            choices.setdefault(row["network_plmn"], "")
        self.network.clear()
        for code, label in sorted(choices.items()):
            if code:
                self.network.addItem(f"{label} ({code})" if label else code, code)
        self.network.setCurrentIndex(-1)
        self.network.setEditText(network)
        self._filling = False

    def _profile_selected(self):
        if self._filling:
            return
        key = self.apn_name.currentData()
        if key is None:
            return
        if key == "@current":
            self._new_apn = False
            self._saved_profile_id = ""
            self._apn_detached = False
            self._dirty.difference_update({"name", "apn", "username", "password", "authentication", "ip_protocol"})
            self._set_apn((self._observation or {}).get("apn") or {})
        elif key == "@new":
            self._new_apn = True
            self._saved_profile_id = ""
            self._apn_detached = True
            self._set_apn({})
            self._dirty.update({"name", "apn", "username", "password", "authentication", "ip_protocol"})
        elif key in self._library:
            self._new_apn = False
            row = self._library[key]
            self._saved_profile_id = key
            self._apn_detached = True
            self._set_apn(row)
            self._dirty.update({"name", "apn", "username", "authentication", "ip_protocol"})
            self._dirty.discard("password")
            self.apn_password.setPlaceholderText("Saved password" if row["password_known"] else "Password not saved — enter it")
        self._edited("name")

    def _selection_changed(self):
        if not ({"selection", "network"} & self._dirty):
            return False
        return self._bound_selection != (self.selection.currentData(), self.network_code() if self.selection.currentData() == "manual" else "")

    def _apn_changed(self):
        if "password" in self._dirty or ("name" in self._dirty and self._saved_profile_id):
            return True
        baseline = self._bound_apn or {}
        values = {"name": self.apn_name.currentText().strip(), "apn": self.apn_value.text().strip(),
                  "username": self.apn_user.text().strip(), "authentication": self.authentication.currentData(),
                  "ip_protocol": self.protocol.currentData()}
        return any(key in self._dirty and value != (baseline.get(key) or "") for key, value in values.items())

    def _has_changes(self):
        return self._writable and (("roaming" in self._dirty and self.roaming.currentData() != self._bound_roaming)
                or self._selection_changed() or self._apn_changed())

    def draft(self):
        if not self._editable():
            return None
        self._normalize_network_choice()
        if not self._has_changes():
            return None
        roaming = self.roaming.currentData() if "roaming" in self._dirty else None
        selection = self.selection.currentData() if self._selection_changed() or roaming is True else "keep"
        apn = None
        if self._apn_changed():
            apn = {"profile_id": "@new" if self._new_apn and self._profile_creation else "@active",
                   "name": self.apn_name.currentText().strip(), "apn": self.apn_value.text().strip(),
                   "username": self.apn_user.text().strip(), "authentication": self.authentication.currentData(),
                   "ip_protocol": self.protocol.currentData(),
                   "protected_credentials": seal_apn_credentials(self.apn_user.text().strip(), self.apn_password.text() if "password" in self._dirty else None)}
        return {"schema": "NetworkConnectDraft.v1", "intention": {"schema": "PhysicalSimNetworkIntention.v1",
                              "sim_country": self.sim_country.currentData() or "",
                              "roaming_enabled": roaming, "selection": selection, "home_provider": "",
                              "roaming_country": self.visited_country.currentData() if roaming else "",
                              "target_plmn": (self.network_code() or self.network.currentText().strip()) if selection == "manual" else ""},
                "subscription_key": "" if self._new_apn else self._bound_subscription, "apn": apn,
                "network_plmn": self.network_code(),
                "saved_profile_id": self._saved_profile_id if "password" not in self._dirty else ""}

    def _validate_disconnected_fields(self):
        draft = self.draft()
        if draft is None:
            return
        try:
            validate_connect_network_draft(draft)
        except ValueError as exc:
            self.feedback.setText(connect_network_input_message(str(exc), draft))

    def _visibility(self):
        editable = self._editable()
        self.apply.setVisible(self._state == "connected" and self._writable)
        self.apply.setEnabled(editable and self._state == "connected" and self._has_changes())
        for widget in (self.sim_country, self.roaming, self.network, self.visited_country,
                       self.selection, self.apn_name, self.authentication, self.protocol):
            widget.setEnabled(editable)
        for widget in (self.apn_value, self.apn_user, self.apn_password):
            widget.setReadOnly(not editable)
        if self._profile_creation and self._observation and not self._new_apn:
            active = self._observation.get("apn")
            if active and not active["editable"]:
                for widget in (self.apn_value, self.apn_user, self.apn_password):
                    widget.setReadOnly(True)
                self.authentication.setEnabled(False)
                self.protocol.setEnabled(False)
        self.visited_country.setEnabled(editable and self.roaming.currentData() is True)
        if editable and self.roaming.currentData() is False:
            self._filling = True
            self._set_combo(self.visited_country, self.sim_country.currentData() or "")
            self._filling = False
        self.details.layout().labelForField(self.visited_country).setText(
            "Roaming country" if self.roaming.currentData() is True else "Network country")
        self.details.layout().labelForField(self.network).setText(
            "Roaming network" if self.roaming.currentData() is True else "Network")

    def _render_result(self, truth):
        if not self._writable:
            return False
        field = truth.fields.get("manual.lifecycle")
        result = field.value if field and field.state is PresentationState.VALID and isinstance(field.value, Mapping) else {}
        action_id = result.get("action_id", "")
        if (result.get("lever") != "NETWORK_SETTINGS" or not action_id
                or float(result.get("requested_ts") or 0.0) < self._result_since
                or (self._result_action_id and action_id != self._result_action_id)):
            return
        self._result_action_id = action_id
        status = result.get("status")
        if status in {"failed", "rejected", "timed_out", "cancelled", "transport_failed"}:
            self._applying = False
            reason = str(result.get("error") or result.get("message") or "Disconnect to edit and retry.")
            if reason == "apn_explicit_password_required":
                self.apn_password.setPlaceholderText("APN password required to retry")
            self.feedback.setText("Not confirmed: " + network_failure_text(reason))
            if reason == "accepted_deadline_expired":
                self.feedback.setText(self.feedback.text() + "\n\n" + self._reported_settings_text())
        elif status == 'unchanged' and not result.get('action_pending'):
            if self._state != 'connected':
                return
            self._submitted_form = None
            self._applying = False
            self._discard_draft()
            self._fill_settings(self._observation)
            self.feedback.setText('Already using these settings. No change was sent.')
        elif (status in {"confirmed", "settled"} and not result.get("action_pending")
              and result.get("target_first_verified_ts") and result.get("canonical_receipt")):
            if self._state != "connected":
                return
            self._submitted_form = None
            self._applying = False
            self._discard_draft()
            self._fill_settings(self._observation)
            self.feedback.setText("Confirmed")
        elif status == "presentation_failed" and not result.get("action_pending"):
            self.feedback.setText("Result unavailable: " + str(result.get("message") or "Check Recent Events."))
        elif result.get("action_pending"):
            if self._state in {"draft", "stopped", "failed", ""}:
                self.feedback.setText("Connection ended. Settings were not confirmed; your choices are kept.")
                return False
            if status in {"accepted", "verifying", "target_pending"}:
                self.feedback.setText("Settings sent. Checking the modem's reported settings and network connection…")
            else:
                self.feedback.setText(network_progress_text(result, now=time()))
            return True
        elif status == "accepted":
            self.feedback.setText("Not confirmed: the modem has not verified the settings.")

    def render(self, truth):
        self._truth = truth
        state = login_lifecycle_state(truth.fields["profile.login_lifecycle"].value)
        previous = self._state
        self._state = state
        if (state in {"draft", "stopped", "failed", ""}
                and previous not in {"draft", "stopped", "failed", ""}):
            # Login can fail or be cancelled before any network command exists.
            # Its canonical terminal state ends the local waiting explanation.
            self.feedback.clear()
            self._applying = False
        if state == "connected":
            imsi = truth.fields.get("device.imsi")
            current = subscription_key(str(imsi.value or "")) if imsi and imsi.state is PresentationState.VALID else ""
            if previous != "connected" or (current and current != self._bound_subscription):
                if (self._submitted_form is not None and current and self._submitted_form["subscription"]
                        and current != self._submitted_form["subscription"]):
                    self._submitted_form = None
                self._discard_draft()
                self._modem_values.clear()
                self._observation = None
                self._bound_apn = self._bound_selection = self._bound_roaming = None
                self._bound_subscription = current
            for name in ("sim.network", "provider.plmn", "provider.name"):
                field = truth.fields.get(name)
                if field and field.state is PresentationState.VALID:
                    if name != "sim.network" or field.value.get("subscription_key") == current:
                        self._modem_values[name] = field.value
            field = truth.fields["network.settings"]
            obs = field.value if field.state is PresentationState.VALID and isinstance(field.value, Mapping) else None
            if obs and current and obs.get("subscription_key") == current:
                self._observation = obs
                self._bound_apn = dict(obs["apn"]) if obs["apn"] else None
                self._bound_selection = (obs["selection"] or "", (obs["target_plmn"] or "") if obs["selection"] == "manual" else "")
                self._bound_roaming = obs["roaming_enabled"]
            if self._submitted_form is not None:
                self._fill_settings(self._observation)
                self._show_submitted_form(self._submitted_form)
            else:
                self._fill_settings(self._observation)
        else:
            # Disconnection keeps the last known form and the local draft. No read is needed to edit.
            if previous == "connected":
                self._restore_submitted_form()
            self._refresh_profiles()
        command_pending = self._render_result(truth)
        if not self._writable:
            status = ("SIM, network and APN settings are read-only in 54GUC. "
                      "Change them in the router's own settings. "
                      + ("The fields show the last received settings." if self._observation else
                         "Click Connect to read the router's settings." if state in {"draft", "stopped", "failed", ""} else
                         "Available settings appear as they arrive."))
        elif state == "connected":
            sim = truth.fields.get("sim.network")
            current_settings = bool(current and sim and sim.state is PresentationState.VALID
                and sim.value.get("subscription_key") == current
                and obs and obs.get("subscription_key") == current)
            status = ("Your requested settings are shown; they are not yet confirmed by the router."
                      if self._submitted_form is not None else
                      ("Mobile data is off. Edit settings and click Apply settings. Choose NEW for a separate APN profile."
                       if self._mobile_off() else "Disconnect mobile data to edit SIM, network or APN settings. Keep the device connected.") if current_settings else
                      "Last known settings are shown; they have not been rechecked since the interruption. Disconnect to make changes."
                      if self._observation else
                      "Reading network and APN settings; available fields appear as they arrive.")
        elif state in {"draft", "stopped", "failed", ""}:
            status = ("Leave the fields unchanged to use the router's existing settings, then click Connect. "
                      "To request changes, choose AUTO or a specified network. For roaming, select the SIM country "
                      "and network country. Fill APN details only if you want to change the APN profile. "
                      "The active SIM is checked before applying changes.")
        else:
            status = "Connecting and checking the active SIM; available settings appear as they arrive."
        self.inspection_status.setText(status)
        diagnostic = truth.fields.get("network.diagnostic")
        mobile = truth.fields.get("device.modem_status")
        text = ""
        if state == "connected" and not command_pending and diagnostic and diagnostic.state is PresentationState.VALID:
            text = network_diagnostic_text(diagnostic.value, subscription=self._bound_subscription,
                mobile_status=mobile.text if mobile and mobile.state is PresentationState.VALID else "")
        self.diagnostic.setText(text)
        self.diagnostic.setVisible(bool(text))
        self._visibility()

    def _reported_settings_text(self):
        """Display received facts, without recomputing Core's confirmation."""
        obs = self._observation or {}
        apn = obs.get("apn") or {}
        roaming = {True: "Enabled", False: "Disabled", None: "Not reported"}[obs.get("roaming_enabled")]
        return (f"Last router readback: network selection {obs.get('selection') or 'not reported'}; "
                f"configured network {obs.get('target_plmn') or 'not specified'}; roaming {roaming}.\n"
                f"Serving network: {self._field('provider.name') or 'not reported'} "
                f"({self._field('provider.plmn') or 'code not reported'}).\n"
                f"APN profile: {apn.get('name') or 'not reported'}; APN: {apn.get('apn') or 'not reported'}; "
                f"authentication: {apn.get('authentication') or 'not reported'}; "
                f"IP protocol: {apn.get('ip_protocol') or 'not reported'}. "
                "These readings do not by themselves confirm your requested changes.")
