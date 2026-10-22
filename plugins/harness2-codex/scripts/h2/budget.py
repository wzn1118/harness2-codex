"""Preallocated resource envelopes: validation arms cannot spend each other's quota."""
from __future__ import annotations

import threading


def allocation(request):
    repeats = request.get('validation_repeats', 2)
    limits = {'requests': request.get('max_requests', 160),
              'output': request.get('max_output_tokens', 655360),
              'tokens': request.get('max_total_tokens', 10000000)}
    slots = 2 * repeats + 4
    unit = {key: value // slots for key, value in limits.items()}
    plan = {'reflection': dict(unit)}
    for trial in range(repeats):
        for arm in ('baseline', 'candidate'):
            plan[f'holdout-{trial}-{arm}'] = dict(unit)
    plan['optimization'] = {key: value - unit[key] * (2 * repeats + 1)
                            for key, value in limits.items()}
    return limits, plan


class Budget:
    def __init__(self, limits, plan):
        self.limits, self.plan = limits, plan
        self.used = {name: dict.fromkeys(limits, 0) for name in plan}
        self.lock = threading.RLock()

    def scope(self, label):
        if label.startswith('holdout-'):
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
        actual = int(usage.get('total_tokens', 0))
        if not actual or actual > reservation['tokens']:
            raise ValueError('Provider usage exceeds conservative request reservation')
        with self.lock:
            self.used[reservation['scope']]['tokens'] -= reservation['tokens'] - actual

    def ledger(self):
        with self.lock:
            return {'limits': dict(self.limits), 'allocations': self.plan,
                    'charged': {name: dict(values) for name, values in self.used.items()},
                    'token_accounting': 'provider total_tokens when reported; otherwise UTF-8 request bytes + 8192 framing + reserved output; output reservations are not refunded'}
