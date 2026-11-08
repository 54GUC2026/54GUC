"""Fixed-hour production contract: coverage, identity, aging and replay."""

from copy import deepcopy
from datetime import datetime, timezone
import inspect

import pytest

from arch4.new_horizon.learning_context import build_learning_context
from arch4.new_horizon.hourly_aq import (
    aggregate_aq_blocks, make_aq_block, hourly_aq_advice, hourly_observed_vector, fixed_hour,
)
from arch4.new_horizon import hourly_aq


ZONE = 'UTC'


def test_hourly_advice_validates_once_then_uses_one_vector_owner():
    advice = inspect.getsource(hourly_aq.hourly_aq_advice)
    public_vector = inspect.getsource(hourly_aq.hourly_observed_vector)
    assert "hour = immutable_aq_hour(raw)" in advice
    assert "hourly_observed_vector(hour)" not in advice
    assert "_hourly_observed_vector_validated(hour)" in advice
    assert "_hourly_observed_vector_validated(immutable_aq_hour(hour))" in public_vector
    assert "require_aq_hour(value)" in inspect.getsource(hourly_aq.immutable_aq_hour)
    assert inspect.getsource(hourly_aq).count("def _hourly_observed_vector_validated(") == 1


def stamp(hour, minute=0):
    return datetime(2026,9,6,hour,minute,tzinfo=timezone.utc).timestamp()


def identity(cell=2050):
    return dict(network_type='LTE (4G)',pband=3,ca_pool=[1,3,20],cell_id=cell,
        pci=255 if cell==2050 else 153,earfcn=1795 if cell==2050 else 1651,
        nr_band=None,nr_cell_id=None,nr_pci=None,nr_arfcn=None,
        scope_key=f'exact:{cell}',target_signature='PBAND=B3|CA=B1+B3+B20')


def block(key, start, aq, *, cell=2050, vector=None):
    return make_aq_block(assessment_id=key,continuity_key='device:A1',
        identity=identity(cell),opened_at=start,ended_at=start+600,aq=aq,
        observed_vector=vector or {'eci_points':30,'latency_ms':30,'cqb_cell':.8})


def proof(key, start, *, eci=30, cell=2050):
    return dict(assessment_id=key,continuity_key='device:A1',
        subject_kind='active_context_window', opened_at=start,due_at=start+600,
        closed_at=start+600, result_identity=identity(cell),result_scope_key=f'exact:{cell}',
        target_signature='PBAND=B3|CA=B1+B3+B20',
        learning_context=build_learning_context(exact_scope_key=f'exact:{cell}',observed_at=start),
        observed_vector={'eci_points':eci,'latency_ms':30,'jitter_ms':5,'cqb_cell':.8})


def test_repeated_visits_weight_real_coverage_not_hour_or_visit_count():
    rows=[block('a',stamp(10),3),block('b',stamp(10,10),3),block('c',stamp(10,50),0)]
    hours,excluded=aggregate_aq_blocks(rows,timezone=ZONE)
    assert not excluded and len(hours)==1
    assert hours[0]['coverage_s']==1800
    assert hours[0]['aq']==pytest.approx(2)
    assert aggregate_aq_blocks(reversed(rows),timezone=ZONE)==(hours,excluded)
    assert aggregate_aq_blocks(rows+rows,timezone=ZONE)==(hours,excluded)


def test_boundary_exclusion_does_not_discard_neighbors_or_invent_nine_minutes():
    rows=[block('a',stamp(10,39),2),block('crossing',stamp(10,59),1),block('b',stamp(11,9),3)]
    with pytest.raises(ValueError,match='cross_hour'):
        aggregate_aq_blocks(rows,timezone=ZONE)
    hours,excluded=aggregate_aq_blocks(rows,timezone=ZONE,discard_legacy_crossings=True)
    assert sum(h['coverage_s'] for h in hours)==1200
    assert excluded==('device:A1:crossing',)
    with pytest.raises(ValueError,match='ten_minute'):
        make_aq_block(assessment_id='nine',continuity_key='device:A1',identity=identity(),
            opened_at=stamp(11),ended_at=stamp(11,9),aq=1,observed_vector={})


def test_distinct_cells_never_become_one_hour_and_overlap_never_inflates_coverage():
    a=block('a',stamp(10),3)
    b=block('b',stamp(10),-3,cell=2070)
    hours,_=aggregate_aq_blocks([a,b],timezone=ZONE)
    assert len(hours)==2
    assert sorted(h['aq'] for h in hours)==[-3,3]
    with pytest.raises(ValueError,match='overlapping'):
        aggregate_aq_blocks([a,block('overlap',stamp(10,5),3)],timezone=ZONE)
    changed=deepcopy(a);changed['aq']=0
    with pytest.raises(ValueError,match='identity_reused'):
        aggregate_aq_blocks([a,changed],timezone=ZONE)


def test_missing_metric_has_own_denominator_and_counter_is_not_averaged():
    hours,_=aggregate_aq_blocks([
        block('a',stamp(10),1,vector={'latency_ms':20,'jitter_ms':2,'sample_count':300}),
        block('b',stamp(10,10),1,vector={'latency_ms':40,'jitter_ms':None,'sample_count':280}),
    ],timezone=ZONE)
    vector=hourly_observed_vector(hours[0])
    assert vector=={'latency_ms':30,'jitter_ms':2,'sample_count':580}


def test_aging_cannot_rewrite_aq_or_give_future_hours_influence():
    hours,_=aggregate_aq_blocks([block('a',stamp(10),2)],timezone=ZONE)
    original=deepcopy(hours)
    assert not hourly_aq_advice(hours,now=stamp(10,9))
    fresh=hourly_aq_advice(hours,now=stamp(10,10))[0]
    older=hourly_aq_advice(hours,now=stamp(10,10)+7*86400)[0]
    assert fresh['aq']==older['aq']==2
    assert older['effective_blocks']==pytest.approx(fresh['effective_blocks']/2)
    assert not hourly_aq_advice(hours,now=stamp(10,10)+14*86400)
    assert hours==original


def test_multiple_days_produce_24_separate_clock_hour_predictions():
    rows=[]
    for day in (0,86400):
        rows.extend(block(f'{day}:{h}',stamp(h)+day,1) for h in range(24))
    hours,_=aggregate_aq_blocks(rows,timezone=ZONE)
    advice=hourly_aq_advice(hours,now=stamp(0)+2*86400)
    assert len(hours)==48 and len(advice)==24
    assert {a['time_bucket'] for a in advice}=={f'{h:02d}' for h in range(24)}
    assert all(a['dated_hours']==2 and a['coverage_s']==1200 for a in advice)


def test_replay_does_not_peek_at_future_result_or_assume_missing_forecasts_are_zero():
    first,_=aggregate_aq_blocks([block('first',stamp(10),1)],timezone=ZONE)
    future,_=aggregate_aq_blocks([block('future',stamp(11),3)],timezone=ZONE)
    assert not hourly_aq_advice(first,now=stamp(10))
    assert hourly_aq_advice(first+future,now=stamp(10,10)) == hourly_aq_advice(first,now=stamp(10,10))


def test_exclusion_can_remove_the_only_current_cell_evidence_not_just_storage():
    hours,excluded=aggregate_aq_blocks([
        block('other_cell',stamp(12,30),3,cell=2050),
        block('exact_crossing',stamp(12,55),1,cell=2070),
    ],timezone=ZONE,discard_legacy_crossings=True)
    assert excluded == ('device:A1:exact_crossing',)
    advice=hourly_aq_advice(hours,now=stamp(13,5))
    assert len(advice)==1 and advice[0]['identity']['cell_id']==2050


@pytest.mark.parametrize("stamp", [1.0, 102.0, 3600.0, 1788854400.0])
def test_local_fixed_hour_handles_epoch_and_current_clock_on_windows(stamp):
    start, end, label, local_hour = fixed_hour(stamp)
    assert start <= stamp < end
    assert end - start == 3600
    local = datetime.fromisoformat(label)
    assert local.timestamp() == start
    assert local.hour == local_hour
    assert local.minute == local.second == local.microsecond == 0
