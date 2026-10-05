import json
from pathlib import Path
import shutil
import subprocess
import unittest

from jinja2 import Environment, FileSystemLoader, select_autoescape


ROOT = Path(__file__).resolve().parent


class StageButtonTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js required for JavaScript regression test')
    def test_rendered_buttons_send_commands_despite_native_command_property(self):
        env = Environment(loader=FileSystemLoader(ROOT / 'templates'), autoescape=select_autoescape())
        cases = []
        for template in ('producao_operador.html', 'producao_apontamento.html'):
            for state in ('pending', 'running', 'paused', 'completed', 'setup', 'rejected'):
                stage = {
                    'stage_code': 'REVEST', 'input_code': 'S' if state == 'completed' else 'P' if state in ('running', 'paused', 'setup') else 'N',
                    'open_session': state == 'running', 'open_pause': state == 'paused', 'open_setup': state == 'setup',
                    'active_interval_id': 'test-interval', 'active_interval_start_input': '2026-10-05T08:00:00',
                }
                if state == 'running':
                    actions = ['SETUP', 'PARAR', 'INTERROMPER', 'FINALIZAR']
                elif state == 'setup':
                    actions = ['INICIAR', 'PARAR', 'INTERROMPER', 'FINALIZAR']
                else:
                    actions = ['INICIAR']
                cases.append({
                    'name': f'{template}:{state}', 'actions': actions, 'status': stage['input_code'], 'reject': state == 'rejected',
                    'html': env.get_template(template).render(
                        stage=stage, current_user={'nome': 'Operador'},
                        detail={'target_kind': 'work', 'target_id': 'test-work', 'item_number': 1, 'chassi': 'TEST'},
                    ),
                })
                if template == 'producao_operador.html' and state in ('running', 'setup'):
                    cases.append({**cases[-1], 'finishMode': 'adjust'})
                    cases.append({**cases[-1], 'finishMode': 'end-only'})
                if template == 'producao_operador.html' and state == 'pending':
                    cases.append({**cases[-1], 'startOverride': '2026-10-05T07:00:00'})
                if state == 'running':
                    for invalid in ('Internal Server Error', '<html>Bad Gateway</html>', '', 'null', '[]'):
                        cases.append({**cases[-1], 'invalidResponse': invalid, 'reject': True})
        result = subprocess.run(
            [shutil.which('node'), str(ROOT / 'tests' / 'mes_stage_buttons.cjs')],
            input=json.dumps(cases), text=True, encoding='utf-8', capture_output=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
