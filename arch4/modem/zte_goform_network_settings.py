"""Qualified GoForm network operations, within the selected profile contract."""
import hashlib
import json
import time
import uuid
from arch4.modem.network_inspection import NetworkSettingsRead
from arch4.core_contracts.network_settings import (CoreNetworkSettingsTarget, CoreNsaRegistrationGuard,
    subscription_key, validate_network_observation, nsa_registration_matches)
from arch4.modem.zte_parsers import CONNECTED_PPP_STATES

NSA_CONTRACT='stock_goform_lte_first_nsa_v1'
REGISTRATION_FIELDS=('loginfo','model_name','product_name','wa_inner_version','imsi','sim_imsi',
    'ppp_status','net_select','net_select_mode','m_netselect_save','m_netselect_result',
    'rmcc','rmnc','mccmnc','network_provider_fullname','network_type','current_network_mode',
    'lte_band_lock','nr5g_nsa_band_lock','nr5g_sa_band_lock','wan_apn','profile_name',
    'Current_index','ppp_auth_mode','pdp_type')
PRESERVED_FIELDS=('lte_band_lock','nr5g_nsa_band_lock','nr5g_sa_band_lock','wan_apn',
    'profile_name','Current_index','ppp_auth_mode','pdp_type')


def _apn_authentication(value):
    return {'none':'none','pap':'pap','chap':'chap','auto':'auto'}.get(str(value or '').strip().casefold())


def _apn_ip_protocol(value):
    return {'ip':'ipv4','ipv4':'ipv4','ipv6':'ipv6','ipv4v6':'ipv4v6'}.get(str(value or '').strip().casefold())


def registration_contract_enabled(modem):
    definition=getattr(modem,'_definition',None)
    if definition is None or getattr(modem,'_backend_name','')!='mc888':
        return False
    document=definition.snapshot();metadata=document['profile']
    return (document.get('parameters',{}).get('goform_nsa_registration_contract')==NSA_CONTRACT
        and metadata.get('family')=='zte_webui_like' and bool(metadata.get('firmware_exact')))


def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def _registration_read(modem):
    raw=modem._read_goform(REGISTRATION_FIELDS)
    connection=modem._read_goform(('ConnectionMode',),multi_data=False)
    return _registration_observation(modem,raw,connection),raw


def _registration_observation(modem,raw,connection,*,session_imsi=""):
    if not registration_contract_enabled(modem):
        raise RuntimeError('network_nsa_profile_unqualified')
    from arch4.modem_live.radio_mode_configuration import native_mode_for_key,mode_key_for_native
    from arch4.modem_live.profile_diagnostics import decode_native_profile
    document=modem._definition.snapshot();metadata=document['profile'];parameters=document['parameters']
    if (str(raw.get('loginfo')or'').strip().casefold()!='ok' or str(raw.get('wa_inner_version')or'').strip().casefold()!=metadata['firmware_exact'].casefold()
            or not any(token.casefold() in str(raw.get('model_name')or raw.get('product_name')or'').casefold()
                       for token in metadata['model_contains'])):
        raise RuntimeError('network_nsa_authentication_or_identity_changed')
    sim, imsi = _subscription(raw, session_imsi)
    mode_key=parameters.get('nsa_registration_mode_key')
    native=native_mode_for_key(modem._definition,mode_key)
    if native not in getattr(modem,'_goform_registration_options',frozenset()):
        raise RuntimeError('network_nsa_mode_option_unavailable')
    ppp=raw.get('ppp_status')
    if ppp not in CONNECTED_PPP_STATES and ppp!='ppp_disconnected':
        raise RuntimeError('network_nsa_data_state_unavailable')
    selection={'auto_select':'auto','manual_select':'manual'}.get(raw.get('net_select_mode'))
    if selection is None or type(raw.get('net_select'))is not str:
        raise RuntimeError('network_nsa_selection_unavailable')
    preserved={key:dict(present=key in raw,value=raw.get(key))for key in PRESERVED_FIELDS}
    preserved['connection_mode']={key:dict(present=key in connection,value=connection.get(key))
        for key in ('connectionMode','autoConnectWhenRoaming')}
    preserved_key=_digest(dict(profile=modem._definition.content_hash,subscription=sim,settings=preserved))
    initial_key=_digest(dict(preserved=preserved_key,selection=raw['net_select_mode'],
        mode=raw['net_select'],saved_selection=dict(present='m_netselect_save'in raw,value=raw.get('m_netselect_save'))))
    guard=CoreNsaRegistrationGuard(modem._definition.content_hash,initial_key,preserved_key,mode_key,ppp in CONNECTED_PPP_STATES)
    available=modem._definition.available_addresses({'goform':raw})
    plmn=None
    if 'identity.provider_plmn' in available:
        plmn=decode_native_profile(modem._definition,{'goform':raw},addresses={'identity.provider_plmn'}).get('identity.provider_plmn')
    try:observed_mode=mode_key_for_native(modem._definition,raw['net_select'])
    except RuntimeError:observed_mode=None
    # SET_NETWORK success is registration evidence, not an Internet/data-session claim.
    # Use the admitted profile's input rules, including retained LTE_CA/NSA
    # spelling variants. Never create a second, narrower native RAT parser.
    network=decode_native_profile(modem._definition,{'goform':raw},
        addresses={'radio.network_type'}).get('radio.network_type')
    has_lte=(network=='LTE (4G)' or network=='NR NSA (5G)' and raw['net_select']!='Only_LTE')
    registered=bool(plmn) and has_lte and (
        raw.get('m_netselect_result')=='manual_success' or ppp in CONNECTED_PPP_STATES)
    apn = None
    if type(raw.get('wan_apn')) is str and raw['wan_apn']:
        apn = dict(profile_id=str(raw.get('Current_index')or''), name=str(raw.get('profile_name')or''),
            apn=raw['wan_apn'], username='', authentication=_apn_authentication(raw.get('ppp_auth_mode')),
            ip_protocol=_apn_ip_protocol(raw.get('pdp_type')),editable=False)
    observation=validate_network_observation(dict(schema='NetworkSettingsObservation.v1',subscription_key=sim,
        selection=selection,target_plmn=plmn if selection=='manual' else None,
        roaming_enabled={'on':True,'off':False}.get(connection.get('autoConnectWhenRoaming')),apn=apn,
        nsa_registration=dict(guard=guard.to_mapping(),mode_key=observed_mode,serving_plmn=plmn,lte_registered=bool(registered))))
    return NetworkSettingsRead(observation,imsi)


def _bound_registration(modem,plan):
    if (not isinstance(plan.target,CoreNetworkSettingsTarget) or plan.target.selection!='prefer_nsa'
            or not registration_contract_enabled(modem) or plan.stage_kind not in plan.target.stage_kinds):
        raise RuntimeError('network_nsa_profile_or_target_unqualified')
    if len(plan.preceding_network_stages)!=plan.ordinal-1:
        raise RuntimeError('network_nsa_preceding_native_ack_required')
    read,raw=_registration_read(modem)
    # NetworkSettingsRead owns this public sanitized observation; no cached radio values.
    observation=read.observation
    current=CoreNsaRegistrationGuard.from_mapping(observation['nsa_registration']['guard'])
    expected=plan.target.nsa_guard
    if (observation['subscription_key']!=plan.target.subscription_key
            or current.profile_definition_hash!=expected.profile_definition_hash
            or current.preserved_configuration_key!=expected.preserved_configuration_key
            or current.mode_key!=expected.mode_key):
        raise RuntimeError('network_nsa_preserved_configuration_changed')
    if plan.target.roaming_enabled is not None and observation['roaming_enabled']is not plan.target.roaming_enabled:
        raise RuntimeError('network_nsa_roaming_change_requires_router_settings')
    return observation,raw,current


def registration_stage_ready(modem,plan):
    started=float(time.time())
    observation,raw,current=_bound_registration(modem,plan)
    expected=plan.target.nsa_guard
    if plan.stage_kind=='network_operator_lte':
        if current.initial_configuration_key!=expected.initial_configuration_key:
            raise RuntimeError('network_nsa_initial_configuration_changed')
        return raw['ppp_status']=='ppp_disconnected'
    if plan.stage_kind=='network_enable_nsa':
        result=raw.get('m_netselect_result')
        if result=='manual_fail':
            raise RuntimeError('network_nsa_lte_registration_refused')
        return (raw['ppp_status']=='ppp_disconnected' and raw['net_select_mode']=='manual_select'
            and raw['net_select']=='Only_LTE' and result=='manual_success'
            and observation['nsa_registration']['lte_registered']
            and observation['nsa_registration']['serving_plmn']==plan.target.target_plmn)
    if plan.stage_kind=='network_data_start':
        if not nsa_registration_matches(plan.target,observation):
            return False
        if raw['ppp_status']in CONNECTED_PPP_STATES:
            from arch4.modem.backend_acquisition import BackendAcquisitionRequest,capture_backend_acquisition
            request=BackendAcquisitionRequest('network-data-session:'+uuid.uuid4().hex,'post_write',started)
            return capture_backend_acquisition(request=request,backend=modem._backend_name,profile_id=modem._definition.profile_id,
                modem=None,status={'network_settings':observation,'data_session_connected':True},
                acquired_cells=frozenset({'slow.network_settings'}))
        return raw['ppp_status']=='ppp_disconnected'
    raise RuntimeError('network_nsa_readiness_stage_invalid')


def registration_payload(modem,plan):
    observation,raw,current=_bound_registration(modem,plan)
    target=plan.target;expected=target.nsa_guard
    if plan.stage_kind=='network_data_hold':
        if current!=expected or raw['ppp_status']not in CONNECTED_PPP_STATES:
            raise RuntimeError('network_nsa_initial_configuration_changed')
        return {'isTest':'false','goformId':'DISCONNECT_NETWORK','notCallback':'true'}
    if plan.stage_kind=='network_operator_lte':
        if current.initial_configuration_key!=expected.initial_configuration_key or raw['ppp_status']!='ppp_disconnected':
            raise RuntimeError('network_nsa_confirmed_data_off_required')
        return {'isTest':'false','goformId':'SET_NETWORK','NetworkNumber':target.target_plmn,'Rat':'7'}
    if plan.stage_kind=='network_enable_nsa':
        if not (raw['ppp_status']=='ppp_disconnected' and raw['net_select_mode']=='manual_select'
                and raw['net_select']=='Only_LTE' and raw.get('m_netselect_result')=='manual_success'
                and observation['nsa_registration']['lte_registered']
                and observation['nsa_registration']['serving_plmn']==target.target_plmn):
            raise RuntimeError('network_nsa_fresh_lte_registration_required')
        from arch4.modem_live.radio_mode_configuration import native_mode_for_key
        return {'isTest':'false','goformId':'SET_BEARER_PREFERENCE','BearerPreference':native_mode_for_key(modem._definition,expected.mode_key)}
    if plan.stage_kind=='network_data_start':
        if raw['ppp_status']!='ppp_disconnected' or not nsa_registration_matches(target,observation):
            raise RuntimeError('network_nsa_mode_and_operator_confirmation_required')
        return {'isTest':'false','goformId':'CONNECT_NETWORK','notCallback':'true'}
    raise RuntimeError('network_nsa_stage_invalid')


def _read_contract(modem):
    profile = modem._resolve_firmware_profile()
    contract = profile.network_settings if profile is not None else ""
    if contract not in {"goform_mf296r_b04", "goform_mc801a_at_b14_read_assumption", "goform_g5b_b03_read_assumption"}:
        raise RuntimeError("network_inspection_profile_or_subscription_unavailable")
    return contract


def _write_qualified(modem):
    profile = modem._resolve_firmware_profile()
    return profile is not None and profile.network_settings == "goform_mf296r_b04"


def _subscription(raw, session_imsi=''):
    native=[str(raw[key]) for key in ('imsi','sim_imsi') if raw.get(key)]
    keys={subscription_key(value) for value in native}
    if session_imsi:keys.add(subscription_key(session_imsi))
    if len(keys)!=1 or '' in keys:
        raise RuntimeError('network_subscription_changed_or_unavailable')
    return next(iter(keys)), native[0] if native else ''


def _readable_registration_settings(modem, raw, connection, *, session_imsi=''):
    """Reading APN/operator facts does not qualify a radio-changing sequence."""
    metadata=modem._definition.metadata
    if (str(raw.get('loginfo')or'').strip().casefold()!='ok' or str(raw.get('wa_inner_version')or'').strip().casefold()!=metadata['firmware_exact'].casefold()
            or not any(token.casefold() in str(raw.get('model_name')or raw.get('product_name')or'').casefold()
                       for token in metadata['model_contains'])):
        raise RuntimeError('network_nsa_authentication_or_identity_changed')
    sim, imsi = _subscription(raw, session_imsi)
    apn=None
    if type(raw.get('wan_apn'))is str and raw['wan_apn']:
        apn=dict(profile_id=str(raw.get('Current_index')or''),name=str(raw.get('profile_name')or''),
            apn=raw['wan_apn'],username='',
            authentication=_apn_authentication(raw.get('ppp_auth_mode')),
            ip_protocol=_apn_ip_protocol(raw.get('pdp_type')),
            editable=False)
    return NetworkSettingsRead(validate_network_observation(dict(schema='NetworkSettingsObservation.v1',
        subscription_key=sim,
        selection={'auto_select':'auto','manual_select':'manual'}.get(str(raw.get('net_select_mode')or'').strip().casefold()),
        target_plmn=modem._resolve_plmn(raw)or None if str(raw.get('net_select_mode')or'').strip().casefold()=='manual_select'else None,
        roaming_enabled={'on':True,'off':False}.get(str(connection.get('autoConnectWhenRoaming')or'').strip().casefold()),
        apn=apn)),imsi)


def iter_network_settings(modem, *, request=None):
    from arch4.modem_live.profile_definition.native_inputs import native_view
    session_imsi=modem._normalize_imsi(modem._last_status.get('imsi'))
    if registration_contract_enabled(modem):
        fields=tuple(k for k in REGISTRATION_FIELDS if not session_imsi or k not in {'imsi','sim_imsi'})
        raw=native_view(modem._read_goform(fields))
        # Validate the exact physical identity before publishing any child
        # observation. The settings result itself still awaits ConnectionMode.
        initial=_readable_registration_settings(modem,raw,{},session_imsi=session_imsi)
        if initial.imsi:modem._last_status['imsi']=initial.imsi
        if request is not None:
            addresses=frozenset({'identity.imsi','identity.provider_plmn','radio.carrier_count',
                'radio.configured_band_pool','radio.configured_band_pool_signature',
                'radio.configured_band_pool_source','radio.selected_band_set'})
            available=modem._definition.available_addresses({'goform':raw},addresses=addresses)
            values=modem._decode_profile_values({'goform':raw},available)
            values={key:value for key,value in values.items()if value is not None and value!='' and value!=[]}
            if values:
                yield modem._capture_backend_acquisition_receipt(request=request,modem=None,
                    acquired_cells=frozenset(values),
                    status_override={key.split('.',1)[1]:value for key,value in values.items()})
            else:
                yield None
        else:
            yield None
        connection=native_view(modem._read_goform(('ConnectionMode',),multi_data=False))
        result=_readable_registration_settings(modem,raw,connection,session_imsi=session_imsi)
        if modem._goform_registration_options:
            try:
                result=_registration_observation(modem,raw,connection,session_imsi=session_imsi)
            except RuntimeError as exc:
                # A control proof may be unavailable while independent APN/
                # roaming facts are readable. Omit the proof, never invent it.
                # Authentication, device/SIM mismatch and other failures stay
                # failures; command preparation still calls the strict reader.
                if str(exc) not in {'network_nsa_mode_option_unavailable',
                        'network_nsa_data_state_unavailable','network_nsa_selection_unavailable'}:
                    raise
        # Its native SIM receipt was already emitted with the first packet.
        return NetworkSettingsRead(result.observation,'' if request is not None else result.imsi,result.diagnostic)
    contract = _read_contract(modem)
    connection = modem._read_goform(("ConnectionMode",), multi_data=False)
    yield None
    fields = ("imsi", "net_select_mode", "wan_apn", "ppp_username", "ppp_auth_mode", "pdp_type", "profile_name", "Current_index")
    if contract in {"goform_mc801a_at_b14_read_assumption", "goform_g5b_b03_read_assumption"}:
        fields += ("sim_imsi",)
    if session_imsi:fields=tuple(k for k in fields if k not in {"imsi","sim_imsi"})
    raw = modem._read_goform(fields)
    if not connection and not raw:
        return None
    native_sims = {modem._normalize_imsi(raw.get(key)) for key in fields if key in {"imsi", "sim_imsi"}}
    native_sims.discard("")
    if len(native_sims) > 1:
        raise RuntimeError("network_subscription_conflicting_native_aliases")
    imsi = next(iter(native_sims), "")
    if session_imsi and imsi and imsi != session_imsi:raise RuntimeError("network_subscription_changed_or_unavailable")
    apn = None
    if raw.get("wan_apn"):
        apn = {"profile_id": str(raw.get("Current_index") or ""), "name": str(raw.get("profile_name") or ""),
               "apn": str(raw["wan_apn"]), "username": str(raw.get("ppp_username") or ""),
               "authentication": _apn_authentication(raw.get("ppp_auth_mode")),
               "ip_protocol": _apn_ip_protocol(raw.get("pdp_type")),
               "editable": False}
    result=NetworkSettingsRead(validate_network_observation({"schema": "NetworkSettingsObservation.v1",
        "subscription_key": subscription_key(imsi or session_imsi),
        "selection": {"auto_select": "auto", "manual_select": "manual"}.get(str(raw.get("net_select_mode") or "").strip().casefold()),
        "target_plmn": None, "roaming_enabled": {"on": True, "off": False}.get(str(connection.get("autoConnectWhenRoaming") or "").strip().casefold()), "apn": apn}), imsi)
    if imsi:modem._last_status['imsi']=imsi
    return result


def network_payload(modem, plan):
    target = plan.target
    if isinstance(target,CoreNetworkSettingsTarget) and target.selection=='prefer_nsa':
        return registration_payload(modem,plan)
    if not isinstance(target, CoreNetworkSettingsTarget) or plan.stage_kind not in target.stage_kinds or not _write_qualified(modem):
        raise RuntimeError("zte_goform_network_exact_b04_contract_required")
    raw = modem._read_goform(("imsi", "ppp_status"))
    if subscription_key(str(raw.get("imsi") or "")) != target.subscription_key:
        raise RuntimeError("network_subscription_changed_or_unavailable")
    if raw.get("ppp_status") != "ppp_disconnected":
        raise RuntimeError("mf296r_network_changes_require_data_disconnected_in_router_webui")
    if target.apn:
        raise RuntimeError("mf296r_apn_write_schema_unqualified")
    if plan.stage_kind == "network_roaming":
        current = modem._read_goform(("ConnectionMode",), multi_data=False)
        if current.get("connectionMode") not in {"auto_dial", "manual_dial"}:
            raise RuntimeError("mf296r_connection_mode_unavailable")
        return {"isTest": "false", "goformId": "SET_CONNECTION_MODE", "ConnectionMode": current["connectionMode"],
                "roam_setting_option": "on" if target.roaming_enabled else "off"}
    # Firmware AUTO operation also rewrites the RAT preference. No authorized
    # standalone AUTO implementation, guessed RAT, or disconnect/reconnect.
    raise RuntimeError("mf296r_operator_operation_unqualified")
