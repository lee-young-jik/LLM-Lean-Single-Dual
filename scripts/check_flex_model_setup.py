"""Check actual outgoing request options offline; no provider calls."""
import json
import os
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from dotenv import dotenv_values
from openai.types.chat import ChatCompletion
from single_orchestrator.model import OpenAICompatibleModelClient
from single_orchestrator.prompts import BuiltPrompt

settings = {k: str(v) for k, v in dotenv_values(ROOT / 'configs/two_goal_experiment.env').items()}
assert settings['ORCHESTRATOR_MODEL'] == 'openai/gpt-6-luna-pro'
assert {settings[k] for k in ('NATURAL_MODEL', 'LEAN_MODEL', 'RETRY_MODEL')} == {'openai/gpt-6-luna'}
calls = []


def create(**kwargs):
    calls.append(kwargs)
    return ChatCompletion.model_validate({
        'id': 'offline', 'object': 'chat.completion', 'created': 0, 'model': kwargs['model'],
        'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant',
                     'content': '{"task":"prove","lean_strategy":"Use its definitions"}'}}],
    })


fake = NS(base_url='https://openrouter.ai/api/v1', chat=NS(completions=NS(create=create)))
with patch.dict(os.environ, {**settings, 'FLEX_TEST_KEY': 'offline-test'}), patch('openai.OpenAI', return_value=fake):
    for model in ('openai/gpt-6-luna-pro', 'openai/gpt-6-luna'):
        client = OpenAICompatibleModelClient(model=model, api_key_env='FLEX_TEST_KEY')
        client.complete(BuiltPrompt(name='orchestrator.strategy', version='v19-lean-strategy-only',
                                   inputs={}, text='Public offline fixture'))
assert len(calls) == 2
for c in calls:
    assert c['extra_body']['provider'] == {'only': ['openai/flex'], 'allow_fallbacks': False, 'require_parameters': True}
assert calls[0]['model'] == 'openai/gpt-6-luna-pro' and 'reasoning' not in calls[0]['extra_body']
assert calls[1]['extra_body']['reasoning'] == {'effort': 'none'}
print('PASS: Luna Pro planner, Luna workers with reasoning=none; both pinned to openai/flex without fallback. Zero API calls.')
