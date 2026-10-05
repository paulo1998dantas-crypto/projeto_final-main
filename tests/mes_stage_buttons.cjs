const assert = require('node:assert/strict');
const vm = require('node:vm');

async function verify(testCase) {
  const actions = [...testCase.html.matchAll(/data-stage-action="([A-Z_]+)"/g)].map(match => match[1]);
  assert.deepEqual(actions, testCase.actions);
  assert.ok(!testCase.html.includes('onclick="command('));
  for (const action of actions) {
    const buttons = actions.map(stageAction => ({
      command: '', // HTMLButtonElement.command, introduced in Chrome 135.
      dataset: { stageAction }, disabled: false,
      addEventListener(event, handler) { assert.equal(event, 'click'); this.handler = handler; },
    }));
    const elements = {
      message: { textContent: '', hidden: true }, observacoes: { value: 'Teste de interface' },
      inicio: { value: '2026-10-05T08:00' }, momento: { value: '2026-10-05T09:00' },
      responsavel: { value: 'Operador informado' }, 'start-override': { value: testCase.startOverride || '' },
      'finish-dialog': { open: false, showModal() { this.open = true; }, close() { this.open = false; } },
      'finish-error': { hidden: true }, 'finish-adjustments': { hidden: true },
      'finish-start': {
        _value: '', get value() { return this._value; },
        set value(value) { this._value = value.replace(/:00$/, ''); }, focus() {},
      }, 'finish-end': { value: '' },
    };
    for (const id of ['finish-direct', 'finish-review', 'finish-save', 'finish-cancel']) {
      elements[id] = { hidden: false, addEventListener(event, handler) { this.handler = handler; } };
    }
    const requests = [];
    let reloads = 0;
    const context = vm.createContext({
      document: {
        querySelectorAll: () => buttons,
        getElementById: id => elements[id],
      },
      crypto: { randomUUID: () => 'test-button-key' },
      confirm: () => true,
      location: { reload() { reloads++; } },
      fetch: async (endpoint, options) => {
        requests.push({ endpoint, options, payload: JSON.parse(options.body) });
        return { ok: !testCase.reject, json: async () => ({ ok: !testCase.reject, error: 'Etapa alterada' }) };
      },
    });
    for (const script of testCase.html.matchAll(/<script>([\s\S]*?)<\/script>/g)) {
      vm.runInContext(script[1], context);
    }
    await buttons.find(button => button.dataset.stageAction === action).handler();
    const operator = testCase.html.includes('id="finish-dialog"');
    if (operator && action === 'FINALIZAR') {
      assert.equal(requests.length, 0, 'Opening finish dialog must not mutate the stage');
      assert.equal(elements['finish-dialog'].open, true);
      if (['adjust', 'end-only'].includes(testCase.finishMode)) {
        await elements['finish-review'].handler();
        assert.equal(elements['finish-adjustments'].hidden, false);
        if (testCase.finishMode === 'adjust') elements['finish-start'].value = '2026-10-05T08:15:00';
        elements['finish-end'].value = '2026-10-05T07:00:00';
        await elements['finish-save'].handler();
        assert.equal(requests.length, 0, 'Invalid finish range must not reach the API');
        assert.equal(elements['finish-error'].hidden, false);
        elements['finish-end'].value = '2026-10-05T09:00:00';
        await elements['finish-save'].handler();
      } else {
        await elements['finish-cancel'].handler();
        assert.equal(elements['finish-dialog'].open, false);
        assert.equal(requests.length, 0, 'Cancel must not mutate the stage');
        await buttons.find(button => button.dataset.stageAction === action).handler();
        await elements['finish-direct'].handler();
      }
    }
    assert.equal(requests.length, 1, `${testCase.name}: ${action} must reach the API exactly once`);
    assert.equal(requests[0].endpoint, '/api/erp/producao/work/test-work/commands');
    assert.equal(requests[0].options.method, 'POST');
    assert.equal(requests[0].payload.action, action);
    assert.equal(requests[0].payload.stage_code, 'REVEST');
    assert.equal(requests[0].payload.expected_status, testCase.status);
    assert.equal(requests[0].payload.observacoes, 'Teste de interface');
    if (operator) {
      assert.equal(requests[0].payload.responsavel, 'Operador informado');
      if (action === 'INICIAR') assert.equal(requests[0].payload.inicio, testCase.startOverride || undefined);
      if (action === 'FINALIZAR') {
        assert.equal(requests[0].payload.expected_interval_id, 'test-interval');
        if (['adjust', 'end-only'].includes(testCase.finishMode)) {
          assert.equal(requests[0].payload.ajustar_horarios, true);
          if (testCase.finishMode === 'adjust') assert.equal(requests[0].payload.inicio_sessao, '2026-10-05T08:15');
          else assert.ok(!('inicio_sessao' in requests[0].payload), 'Changing only end must preserve exact stored start');
          assert.equal(requests[0].payload.termino, '2026-10-05T09:00:00');
        } else {
          assert.ok(!('inicio_sessao' in requests[0].payload));
          assert.ok(!('termino' in requests[0].payload));
        }
      }
    }
    assert.equal(reloads, testCase.reject ? 0 : 1);
    if (testCase.reject) {
      assert.equal(elements.message.textContent, 'Etapa alterada');
      assert.equal(elements.message.hidden, false);
      assert.ok(buttons.every(button => !button.disabled));
    }
  }
}

let input = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', chunk => input += chunk);
process.stdin.on('end', async () => {
  try {
    // Reproduce the old inline-handler collision without any browser or network.
    assert.throws(() => new Function('button', 'with(button){command("INICIAR")}')({command: ''}), TypeError);
    const cases = JSON.parse(input);
    for (const testCase of cases) await verify(testCase);
    console.log(`${cases.length} rendered stage-button scenarios passed`);
  } catch (error) { console.error(error); process.exitCode = 1; }
});
