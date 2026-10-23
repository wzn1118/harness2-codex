"""Preallocated resource envelopes: validation arms cannot spend each other's quota."""
from __future__ import annotations

import threading


def allocation(request):
    repeats = request.get('validation_repeats', 2)
    validation_tasks = request.get('validation_tasks') or [
        {'task_id': 'primary'}
    ]
    limits = {'requests': request.get('max_requests', 160),
              'output': request.get('max_output_tokens', 655360),
              'tokens': request.get('max_total_tokens', 10000000)}
    validation_slots = 2 * repeats * len(validation_tasks)
    slots = validation_slots + 4
    unit = {key: value // slots for key, value in limits.items()}
    plan = {'reflection': dict(unit)}
    for task in validation_tasks:
        for trial in range(repeats):
            for arm in ('baseline', 'candidate'):
                label = (f"holdout-{trial}-{arm}" if len(validation_tasks)==1 and task['task_id']=='primary'
                         else f"holdout-{task['task_id']}-{trial}-{arm}")
                plan[label] = dict(unit)
    plan['optimization'] = {key: value - unit[key] * (validation_slots + 1)
                            for key, value in limits.items()}
    return limits, plan


def minimum_session_budget_ok(plan):
    validation=[values for label,values in plan.items() if label.startswith('holdout-')]
    if not validation:
        return False
    return all(row['requests']>=4 and row['output']>=16384 and row['tokens']>=100000
               for row in validation)


class Budget:
    def __init__(self, limits, plan):
        self.limits, self.plan = limits, plan
        self.used = {name: dict.fromkeys(limits, 0) for name in plan}
        self.usage_adjustments = []
        self.lock = threading.RLock()

    def scope(self, label):
        if label.startswith('holdout-'):
            base_label, separator, retry_number = label.rpartition('.retry')
            if separator and retry_number.isdigit() and base_label in self.plan:
                return base_label
            if label not in self.plan:
                raise ValueError('Unallocated validation session')
            return label
        return 'reflection' if label.startswith('improve-experience.') else 'optimization'

    def reserve(self, label, input_bound):
        scope = self.scope(label)
        with self.lock:
            used, cap = self.used[scope], self.plan[scope]
            # UTF-8 request bytes conservatively cover client-visible text
            # tokenization, plus an explicit allowance for provider framing.
            output = min(4096, cap['output'] - used['output'])
            tokens = input_bound + output
            charge = {'requests': 1, 'output': output, 'tokens': tokens}
            if output < 1 or any(used[key] + value > cap[key] for key, value in charge.items()):
                raise ValueError('Preallocated ' + scope + ' resource budget exhausted')
            for key, value in charge.items():
                used[key] += value
            return {'scope': scope, 'output': output, 'tokens': tokens}

    def settle(self, reservation, usage):
        if not usage:
            return
        actual = usage.get('total_tokens')
        output = usage.get('output_tokens', 0)
        if type(actual) is not int or actual <= 0 or type(output) is not int or not 0 <= output <= actual:
            raise ValueError('Invalid provider usage; request reservation retained')
        with self.lock:
            scope = reservation['scope']
            used, cap = self.used[scope], self.plan[scope]
            delta = actual - reservation['tokens']
            output_delta = max(0, output - reservation['output'])
            # Reported usage has already been incurred, including any overrun.
            used['tokens'] += delta
            used['output'] += output_delta
            within = all(used[key] <= cap[key] for key in cap)
            adjustment = None
            if delta > 0 or output_delta:
                adjustment = {'scope': scope, 'reserved_tokens': reservation['tokens'],
                              'reported_tokens': actual, 'token_delta': delta,
                              'reserved_output_tokens': reservation['output'],
                              'reported_output_tokens': output,
                              'within_allocation': within, 'charged_after': dict(used)}
                self.usage_adjustments.append(adjustment)
            if not within:
                raise ValueError('Preallocated ' + scope + ' resource budget exhausted by reported provider usage')
            return adjustment

    def ledger(self):
        with self.lock:
            return {'limits': dict(self.limits), 'allocations': self.plan,
                    'charged': {name: dict(values) for name, values in self.used.items()},
                    'usage_adjustments': [dict(item) for item in self.usage_adjustments],
                    'token_accounting': 'provider total_tokens when reported, including above-estimate usage; otherwise UTF-8 request bytes + 8192 framing + reserved output; output reservations are not refunded; scope caps are never shared'}
