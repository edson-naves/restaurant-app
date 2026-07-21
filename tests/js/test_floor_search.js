/*
 * Floor plan search rules.
 *
 * The matching logic lives in floor.html and is the one piece of behaviour the
 * Python suites cannot reach. Rather than reimplement it here — which would
 * test a copy and not the code — the parse() and matches() functions are read
 * straight out of the template and evaluated.
 *
 * Run:  node tests/js/test_floor_search.js
 */
const fs = require('fs');
const path = require('path');

const tpl = fs.readFileSync(
  path.join(__dirname, '..', '..', 'web', 'templates', 'floor.html'), 'utf8'
);

// Pull the real functions out of the template, so this breaks if they change
// rather than quietly testing a stale copy of the rules.
function extract(name, signature) {
  const re = new RegExp('function ' + name + '\\(' + signature + '\\) \\{[\\s\\S]*?\\n  \\}');
  const src = tpl.match(re);
  if (!src) {
    console.error(`FAIL  could not find ${name}() in floor.html — did it get renamed?`);
    process.exit(1);
  }
  return src[0];
}

const parse = eval('(' + extract('parse', 'value').replace('function parse', 'function') + ')');
const cardMatches = eval(
  '(' + extract('cardMatches', 'hay, number, value')
    .replace('function cardMatches', 'function') + ')'
);

// Card haystacks carry the table number separately, as the page does.
function matches(card, value) {
  return cardMatches(card.hay, card.number, value);
}

let ok = true;
function check(cond, label, detail) {
  ok = ok && !!cond;
  console.log(`${cond ? 'PASS' : 'FAIL'}  ${label}${detail ? '  -> ' + detail : ''}`);
}

const t12 = { number: '12', hay: 'table 12 main 1st floor sofia martins occupied ord-260721-00012' };
const t21 = { number: '21', hay: 'table 21 patio 1st floor emma wilson ready to pay' };
const t30 = { number: '30', hay: 'table 30 moon blue unassigned free' };
const t120 = { number: '120', hay: 'table 120 sun blue unassigned free' };

check(matches(t12, ''), 'an empty box shows everything');
check(matches(t12, '   '), 'whitespace only shows everything');

check(matches(t12, '12'), 'a number matches its table');
check(!matches(t21, '12'), 'and not another table');

// Words narrow.
check(matches(t12, '12 sofia'), 'two words both matching still match');
check(!matches(t12, '12 emma'), 'a word that does not match rules the card out');
check(!matches(t12, '12 21'), 'two table numbers without a comma match nothing');

// Commas widen — the point of this change.
check(matches(t12, '12, 21, 30'), 'a comma list matches the first');
check(matches(t21, '12, 21, 30'), 'and the second');
check(matches(t30, '12, 21, 30'), 'and the third');
check(!matches({ number: '44', hay: 'table 44 sun blue unassigned free' }, '12, 21, 30'),
      'but not a table outside the list');

// A number means the table number, never digits found elsewhere on the card.
check(!matches(t12, '21'),
      'a number does not match digits inside an order code');
check(!matches(t12, '1'), 'a partial number does not match a longer one');
check(!matches(t120, '12'), 'searching 12 does not pull in 120');
check(matches(t120, '120'), 'the full number does match');
check(!matches(t21, '1'), 'nor do numbers that merely contain the digits');

// The case that started this: 6 must not drag in the sixties.
const t6 = { number: '6', hay: 'table 6 main 1st floor sofia martins occupied' };
const t60 = { number: '60', hay: 'table 60 moon blue unassigned free' };
const t69 = { number: '69', hay: 'table 69 moon blue unassigned free' };
check(matches(t6, '6'), 'searching 6 finds table 6');
check(!matches(t60, '6'), 'and not table 60');
check(!matches(t69, '6'), 'and not table 69');
check(matches(t6, '13, 21, 6'), 'a comma list still finds it exactly');
check(!matches(t60, '13, 21, 6'), 'without dragging in the sixties');

// The two levels combine.
check(matches(t21, '12 sofia, 21 emma'), 'groups can each have several words');
check(!matches(t21, '12 sofia, 21 sofia'),
      'a group only matches when all its words do');

// Tolerated sloppiness: trailing commas and stray spacing.
check(matches(t12, '12,'), 'a trailing comma is ignored');
check(matches(t12, ' ,, 12 , '), 'empty groups are dropped');
check(matches(t12, '12,21'), 'commas without spaces work');

// Case and other fields.
check(matches(t12, 'SOFIA'), 'search is case insensitive');
check(matches(t21, 'ready'), 'status is searchable');
check(matches(t30, 'moon'), 'zone is searchable');
check(matches(t30, 'blue'), 'floor is searchable');
check(matches(t12, 'ord-260721-00012'), 'order code is searchable');
check(matches(t30, 'unassigned'), 'a table with no waiter is findable');

console.log('\nRESULT:', ok ? 'floor search rules hold' : 'FLOOR SEARCH FAILURES');
process.exit(ok ? 0 : 1);
