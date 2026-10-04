#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 TAN Kun

'''Tests for the DeepSeek DSML tool-call accommodation.

DeepSeek flash models sometimes bypass the harness <tool_code> format and emit
their native DSML block:

    <｜｜DSML｜｜ calls>
    <｜｜DSML｜｜ invoke name="bash_tool">
    <｜｜DSML｜｜ parameter name="args" string="false">{"cmdline": "..."}</｜｜DSML｜｜ parameter>
    </｜｜DSML｜｜ invoke>
    </｜｜DSML｜｜ calls>

The accommodation is STRICTLY gated: only provider=="deepseek" with model name
"deepseek-v4-flash" or "deepseek-flash" interprets the block as a tool call;
every other model keeps the hard "Malformed tool call" error.
'''
import types
import unittest

from yaha.core.agent import Agent, _parse_dsml_invoke, _dsml_coerce, _DSML_MODEL_NAMES
from yaha.config.models import ModelConfig


DSML_EXAMPLE = (
    '<｜｜DSML｜｜ calls>\n'
    '<｜｜DSML｜｜ invoke name="bash_tool">\n'
    '<｜｜DSML｜｜ parameter name="args" string="false">'
    '{"cmdline": "cd /tmp && echo \'--- tests dir ---\'; ls -la tests | head -5"}'
    '</｜｜DSML｜｜ parameter>\n'
    '</｜｜DSML｜｜ invoke>\n'
    '</｜｜DSML｜｜ calls>'
)


def _agent(accommodate):
    agent = object.__new__(Agent)
    agent._dsml_accommodate = accommodate
    return agent


# --- the strict gate ---------------------------------------------------------

def test_gate_model_names_are_exact():
    assert _DSML_MODEL_NAMES == frozenset({'deepseek-v4-flash', 'deepseek-flash'})
    print('test_gate_model_names_are_exact OK')


def test_gate_set_only_for_deepseek_flash():
    def build(provider, model):
        agent = object.__new__(Agent)
        cfg = ModelConfig(name='m', provider=provider, model=model)
        agent._dsml_accommodate = (
            cfg.provider == 'deepseek'
            and (cfg.model or '').lower() in _DSML_MODEL_NAMES
        )
        return agent._dsml_accommodate

    assert build('deepseek', 'deepseek-v4-flash') is True
    assert build('deepseek', 'deepseek-flash') is True
    assert build('deepseek', 'deepseek-v4-pro') is False
    assert build('deepseek', 'deepseek-chat') is False
    assert build('gemini', 'deepseek-v4-flash') is False
    assert build('ollama', 'deepseek-v4-flash') is False
    print('test_gate_set_only_for_deepseek_flash OK')


# --- the parser --------------------------------------------------------------

def test_parse_example_invoke():
    parsed = _parse_dsml_invoke(DSML_EXAMPLE)
    assert parsed is not None
    assert parsed['tool'] == 'bash_tool', parsed
    cmdline = parsed['args']['cmdline']
    assert cmdline.startswith('cd /tmp && echo'), cmdline
    assert 'ls -la tests | head -5' in cmdline, cmdline
    print('test_parse_example_invoke OK')


def test_parse_named_parameters_without_args_blob():
    text = (
        '<｜｜DSML｜｜ invoke name="read_file">\n'
        '<｜｜DSML｜｜ parameter name="path" string="true">README.md</｜｜DSML｜｜ parameter>\n'
        '</｜｜DSML｜｜ invoke>'
    )
    parsed = _parse_dsml_invoke(text)
    assert parsed == {'tool': 'read_file', 'args': {'path': 'README.md'}}, parsed
    print('test_parse_named_parameters_without_args_blob OK')


def test_parse_only_first_invoke():
    text = DSML_EXAMPLE + '\n' + DSML_EXAMPLE.replace('bash_tool', 'write_file')
    parsed = _parse_dsml_invoke(text)
    assert parsed['tool'] == 'bash_tool', parsed
    print('test_parse_only_first_invoke OK')


def test_parse_repairs_sloppy_json_like_tool_code_does():
    # Consistent with the <tool_code> path, which also uses json_repair:
    # sloppy-but-recoverable JSON is accepted.
    text = DSML_EXAMPLE.replace('{"cmdline":', '{cmdline:')
    parsed = _parse_dsml_invoke(text)
    assert parsed is not None and parsed['tool'] == 'bash_tool', parsed
    assert 'cmdline' in parsed['args'], parsed
    print('test_parse_repairs_sloppy_json_like_tool_code_does OK')


def test_parse_rejects_non_dict_args():
    text = DSML_EXAMPLE.replace(
        '{"cmdline": "cd /tmp && echo \'--- tests dir ---\'; ls -la tests | head -5"}',
        '[1, 2, 3]',
    )
    assert _parse_dsml_invoke(text) is None
    print('test_parse_rejects_non_dict_args OK')


def test_parse_rejects_non_invoke_dsml():
    assert _parse_dsml_invoke('<｜｜DSML｜｜ thinking>blah</｜｜DSML｜｜ thinking>') is None
    assert _parse_dsml_invoke('plain text, no dsml') is None
    assert _parse_dsml_invoke('') is None
    assert _parse_dsml_invoke(None) is None
    print('test_parse_rejects_non_invoke_dsml OK')


# --- validate_response integration -------------------------------------------

def test_accommodated_model_interprets_dsml_call():
    agent = _agent(True)
    tool, err = Agent.validate_response(agent, DSML_EXAMPLE)
    assert err is None, err
    assert tool is not None and tool['tool'] == 'bash_tool', tool
    assert 'cmdline' in tool['args'], tool
    print('test_accommodated_model_interprets_dsml_call OK')


def test_other_models_keep_malformed_error():
    agent = _agent(False)
    tool, err = Agent.validate_response(agent, DSML_EXAMPLE)
    assert tool is None, tool
    assert err is not None and 'Malformed tool call' in err, err
    print('test_other_models_keep_malformed_error OK')


def test_accommodation_missing_flag_defaults_off():
    # Duck-typed agents without the flag must keep the error path.
    agent = object.__new__(Agent)
    tool, err = Agent.validate_response(agent, DSML_EXAMPLE)
    assert tool is None and err is not None and 'Malformed tool call' in err
    print('test_accommodation_missing_flag_defaults_off OK')


def test_dsml_with_leading_public_text():
    agent = _agent(True)
    tool, err = Agent.validate_response(agent, 'Let me run that.\n' + DSML_EXAMPLE)
    assert err is None and tool is not None and tool['tool'] == 'bash_tool'
    print('test_dsml_with_leading_public_text OK')


def test_non_dict_dsml_args_still_errors_even_when_accommodated():
    agent = _agent(True)
    text = DSML_EXAMPLE.replace(
        '{"cmdline": "cd /tmp && echo \'--- tests dir ---\'; ls -la tests | head -5"}',
        '[1, 2, 3]',
    )
    tool, err = Agent.validate_response(agent, text)
    assert tool is None and err is not None and 'Malformed tool call' in err
    print('test_non_dict_dsml_args_still_errors_even_when_accommodated OK')


# --- DSML string="false" parameter typing ------------------------------------

DSML_SET_EVENT_EXAMPLE = (
    '<｜｜DSML｜｜ calls>\n'
    '<｜｜DSML｜｜ invoke name="set_event_success">\n'
    '<｜｜DSML｜｜ parameter name="names" string="false">["commit_done"]</｜｜DSML｜｜ parameter>\n'
    '<｜｜DSML｜｜ parameter name="summary" string="true">Gated commit done.</｜｜DSML｜｜ parameter>\n'
    '</｜｜DSML｜｜ invoke>\n'
    '</｜｜DSML｜｜ calls>'
)


def test_parse_named_parameter_string_false_parses_list():
    parsed = _parse_dsml_invoke(DSML_SET_EVENT_EXAMPLE)
    assert parsed is not None, parsed
    assert parsed['tool'] == 'set_event_success', parsed
    assert parsed['args']['names'] == ['commit_done'], parsed
    assert isinstance(parsed['args']['names'], list), parsed
    assert parsed['args']['summary'] == 'Gated commit done.', parsed
    print('test_parse_named_parameter_string_false_parses_list OK')


def test_parse_named_parameter_string_true_stays_string():
    text = (
        '<｜｜DSML｜｜ invoke name="read_file">\n'
        '<｜｜DSML｜｜ parameter name="path" string="true">README.md</｜｜DSML｜｜ parameter>\n'
        '</｜｜DSML｜｜ invoke>'
    )
    parsed = _parse_dsml_invoke(text)
    assert parsed == {'tool': 'read_file', 'args': {'path': 'README.md'}}, parsed
    print('test_parse_named_parameter_string_true_stays_string OK')


def test_parse_named_parameter_string_missing_stays_string():
    text = (
        '<｜｜DSML｜｜ invoke name="read_file">\n'
        '<｜｜DSML｜｜ parameter name="path">README.md</｜｜DSML｜｜ parameter>\n'
        '</｜｜DSML｜｜ invoke>'
    )
    parsed = _parse_dsml_invoke(text)
    assert parsed == {'tool': 'read_file', 'args': {'path': 'README.md'}}, parsed
    print('test_parse_named_parameter_string_missing_stays_string OK')


def test_parse_named_parameter_string_false_parses_scalars():
    text = (
        '<｜｜DSML｜｜ invoke name="spawn_parallel_agent">\n'
        '<｜｜DSML｜｜ parameter name="clone_context" string="false">42</｜｜DSML｜｜ parameter>\n'
        '</｜｜DSML｜｜ invoke>'
    )
    parsed = _parse_dsml_invoke(text)
    assert parsed['args']['clone_context'] == 42, parsed
    assert isinstance(parsed['args']['clone_context'], int), parsed
    print('test_parse_named_parameter_string_false_parses_scalars OK')


def test_parse_args_blob_contract_unchanged():
    text = (
        '<｜｜DSML｜｜ invoke name="bash_tool">\n'
        '<｜｜DSML｜｜ parameter name="args" string="false">[1, 2, 3]</｜｜DSML｜｜ parameter>\n'
        '</｜｜DSML｜｜ invoke>'
    )
    assert _parse_dsml_invoke(text) is None
    print('test_parse_args_blob_contract_unchanged OK')


def test_dsml_coerce_falls_back_to_raw_string_on_decode_error():
    from unittest.mock import patch
    with patch('yaha.core.agent.json_repair.loads', side_effect=ValueError('boom')):
        assert _dsml_coerce('[1, 2]', 'false') == '[1, 2]'
    print('test_dsml_coerce_falls_back_to_raw_string_on_decode_error OK')


def test_dsml_coerce_true_and_absent_keep_string():
    assert _dsml_coerce('["a"]', 'true') == '["a"]'
    assert _dsml_coerce('["a"]', '') == '["a"]'
    assert _dsml_coerce('["a"]', 'false') == ['a']
    print('test_dsml_coerce_true_and_absent_keep_string OK')


def test_accommodated_set_event_call_yields_list_names():
    agent = _agent(True)
    tool, err = Agent.validate_response(agent, DSML_SET_EVENT_EXAMPLE)
    assert err is None, err
    assert tool is not None and tool['tool'] == 'set_event_success', tool
    assert tool['args']['names'] == ['commit_done'], tool
    print('test_accommodated_set_event_call_yields_list_names OK')


def test_event_names_from_kwargs_repairs_json_array_string():
    assert Agent._event_names_from_kwargs({'names': '["commit_done"]'}) == ['commit_done']
    print('test_event_names_from_kwargs_repairs_json_array_string OK')


def test_event_names_from_kwargs_keeps_plain_and_list():
    assert Agent._event_names_from_kwargs({'names': 'commit_done'}) == ['commit_done']
    assert Agent._event_names_from_kwargs({'names': ['a', 'b']}) == ['a', 'b']
    print('test_event_names_from_kwargs_keeps_plain_and_list OK')


def test_event_names_from_kwargs_numbered_names_still_work():
    assert Agent._event_names_from_kwargs({'name1': 'a', 'name2': 'b'}) == ['a', 'b']
    print('test_event_names_from_kwargs_numbered_names_still_work OK')


def test_normalise_roundtrip_repairs_string():
    collected = Agent._event_names_from_kwargs({'names': '["commit_done"]'})
    assert Agent._normalise_event_names(collected) == ['commit_done']
    print('test_normalise_roundtrip_repairs_string OK')


def test_multi_name_json_string():
    collected = Agent._event_names_from_kwargs({'names': '["a", "b", "a"]'})
    assert Agent._normalise_event_names(collected) == ['a', 'b']
    print('test_multi_name_json_string OK')


def test_end_to_end_lead_dsml_call_signals_real_event():
    parsed = _parse_dsml_invoke(DSML_SET_EVENT_EXAMPLE)
    collected = Agent._event_names_from_kwargs(parsed['args'])
    signalled = Agent._normalise_event_names(collected)
    assert signalled == ['commit_done'], signalled
    assert signalled != ['["commit_done"]'], signalled
    print('test_end_to_end_lead_dsml_call_signals_real_event OK')


class DsmlAccommodationTests(unittest.TestCase):
    '''Unittest-discoverable wrapper for the regression functions above.'''
    test_gate_model_names_are_exact = staticmethod(test_gate_model_names_are_exact)
    test_gate_set_only_for_deepseek_flash = staticmethod(test_gate_set_only_for_deepseek_flash)
    test_parse_example_invoke = staticmethod(test_parse_example_invoke)
    test_parse_named_parameters_without_args_blob = staticmethod(test_parse_named_parameters_without_args_blob)
    test_parse_only_first_invoke = staticmethod(test_parse_only_first_invoke)
    test_parse_repairs_sloppy_json_like_tool_code_does = staticmethod(test_parse_repairs_sloppy_json_like_tool_code_does)
    test_parse_rejects_non_dict_args = staticmethod(test_parse_rejects_non_dict_args)
    test_parse_rejects_non_invoke_dsml = staticmethod(test_parse_rejects_non_invoke_dsml)
    test_accommodated_model_interprets_dsml_call = staticmethod(test_accommodated_model_interprets_dsml_call)
    test_other_models_keep_malformed_error = staticmethod(test_other_models_keep_malformed_error)
    test_accommodation_missing_flag_defaults_off = staticmethod(test_accommodation_missing_flag_defaults_off)
    test_dsml_with_leading_public_text = staticmethod(test_dsml_with_leading_public_text)
    test_non_dict_dsml_args_still_errors_even_when_accommodated = staticmethod(test_non_dict_dsml_args_still_errors_even_when_accommodated)
    test_parse_named_parameter_string_false_parses_list = staticmethod(test_parse_named_parameter_string_false_parses_list)
    test_parse_named_parameter_string_true_stays_string = staticmethod(test_parse_named_parameter_string_true_stays_string)
    test_parse_named_parameter_string_missing_stays_string = staticmethod(test_parse_named_parameter_string_missing_stays_string)
    test_parse_named_parameter_string_false_parses_scalars = staticmethod(test_parse_named_parameter_string_false_parses_scalars)
    test_parse_args_blob_contract_unchanged = staticmethod(test_parse_args_blob_contract_unchanged)
    test_dsml_coerce_falls_back_to_raw_string_on_decode_error = staticmethod(test_dsml_coerce_falls_back_to_raw_string_on_decode_error)
    test_dsml_coerce_true_and_absent_keep_string = staticmethod(test_dsml_coerce_true_and_absent_keep_string)
    test_accommodated_set_event_call_yields_list_names = staticmethod(test_accommodated_set_event_call_yields_list_names)
    test_event_names_from_kwargs_repairs_json_array_string = staticmethod(test_event_names_from_kwargs_repairs_json_array_string)
    test_event_names_from_kwargs_keeps_plain_and_list = staticmethod(test_event_names_from_kwargs_keeps_plain_and_list)
    test_event_names_from_kwargs_numbered_names_still_work = staticmethod(test_event_names_from_kwargs_numbered_names_still_work)
    test_normalise_roundtrip_repairs_string = staticmethod(test_normalise_roundtrip_repairs_string)
    test_multi_name_json_string = staticmethod(test_multi_name_json_string)
    test_end_to_end_lead_dsml_call_signals_real_event = staticmethod(test_end_to_end_lead_dsml_call_signals_real_event)


if __name__ == '__main__':
    unittest.main()
