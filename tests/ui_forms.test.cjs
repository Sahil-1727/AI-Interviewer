const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function fixture(withStatus = true) {
    const formEvents = {}, windowEvents = {};
    const submit = {disabled: false, setAttribute() {}, removeAttribute() {}};
    const extra = {disabled: false, removeAttribute() {}};
    const status = {hidden: true};
    const input = {type: 'password'};
    const toggle = {hidden: true, attributes: {'aria-controls': 'password'},
        getAttribute(name) {return this.attributes[name];},
        setAttribute(name, value) {this.attributes[name] = value;},
        addEventListener(name, callback) {this[name] = callback;}};
    const form = {dataset: {}, classList: {add() {}, remove() {}},
        querySelectorAll(selector) {return selector === 'button' ? [submit, extra] : [];},
        querySelector(selector) {return selector === '.submit-status' ? (withStatus ? status : null) : submit;},
        addEventListener(name, callback) {formEvents[name] = callback;}};
    const document = {
        addEventListener(name, callback) {callback();},
        querySelectorAll(selector) {return selector === '[data-submit-form]' ? [form] : [toggle];},
        getElementById(id) {return id === 'password' ? input : null;}
    };
    vm.runInNewContext(fs.readFileSync('app/static/js/ui.js', 'utf8'), {
        document, window: {addEventListener(name, callback) {windowEvents[name] = callback;}}
    });
    return {form, formEvents, windowEvents, submit, extra, status, input, toggle};
}

test('submission locks duplicates and Back restores the latest button state', () => {
    const f = fixture();
    f.extra.disabled = true; // A permission control changed after DOMContentLoaded.
    f.formEvents.submit({defaultPrevented: false});
    assert.equal(f.submit.disabled, true);
    assert.equal(f.status.hidden, false);
    let prevented = false;
    f.formEvents.submit({preventDefault() {prevented = true;}});
    assert.equal(prevented, true);
    f.windowEvents.pageshow({persisted: true});
    assert.equal(f.submit.disabled, false);
    assert.equal(f.extra.disabled, true);
    assert.equal(f.status.hidden, true);
    assert.equal(f.form.dataset.submitting, undefined);
});

test('forms without a status element still submit and recover', () => {
    const f = fixture(false);
    assert.doesNotThrow(() => f.formEvents.submit({defaultPrevented: false}));
    assert.doesNotThrow(() => f.windowEvents.pageshow({persisted: true}));
});

test('password visibility control updates its accessible state', () => {
    const f = fixture();
    assert.equal(f.toggle.hidden, false);
    f.toggle.click();
    assert.equal(f.input.type, 'text');
    assert.equal(f.toggle.attributes['aria-pressed'], 'true');
    f.toggle.click();
    assert.equal(f.input.type, 'password');
    assert.equal(f.toggle.textContent, 'Show password');
});
