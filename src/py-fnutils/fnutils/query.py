#+
# Copyright 2015 iXsystems, Inc.
# All rights reserved
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted providing that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR
# IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
# STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING
# IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
#
#####################################################################


import re
import inspect
import dateutil.parser

from six import string_types


operators_table = {
    '=': lambda x, y: x == y,
    '!=': lambda x, y: x != y,
    '>': lambda x, y: x > y,
    '<': lambda x, y: x < y,
    '>=': lambda x, y: x >= y,
    '<=': lambda x, y: x <= y,
    '~': lambda x, y: re.match(x, y),
    'in': lambda x, y: y in x,
    'nin': lambda x, y: y not in x
}


conversions_table = {
    'timestamp': lambda v: dateutil.parser.parse(v)
}


def eval_logic_and(item, lst):
    for i in lst:
        if not eval_tuple(item, i):
            return False

    return True


def eval_logic_or(item, lst):
    for i in lst:
        if eval_tuple(item, i):
            return True

    return False


def eval_logic_nor(item, lst):
    for i in lst:
        if eval_tuple(item, i):
            return False

    return True


def eval_logic_operator(item, *t):
    op, lst = t
    return globals()['eval_logic_{0}'.format(op)](item, lst)


def eval_field_operator(item, t):
    left, op, right = t

    if len(t) == 4:
        right = conversions_table[t[3]](right)

    return operators_table[op](item[left], right)


def eval_tuple(item, t):
    if len(t) == 2:
        return eval_logic_operator(item, t)

    if len(t) in (3, 4):
        return eval_field_operator(item, t)


def matches(obj, *rules):
    fail = False
    for r in rules:
        if not eval_tuple(obj, r):
            fail = True
            break

    return not fail


def partition(s):
    res = re.split(r'(?<!\\)\.', s, maxsplit=1)
    left = res[0].replace(r'\.', '.')
    if len(res) == 1:
        return left, None

    return left, res[1]


def wrap(obj):
    if hasattr(obj, '__getstate__'):
        obj = obj.__getstate__()

    if inspect.isgenerator(obj):
        obj = list(obj)

    if type(obj) in (QueryDict, QueryList):
        return obj

    if isinstance(obj, dict):
        return QueryDict(obj)

    if isinstance(obj, list):
        return QueryList(obj)

    return obj


class QueryList(list):
    def __init__(self, *args, **kwargs):
        super(QueryList, self).__init__(*args, **kwargs)
        for idx, v in enumerate(self):
                self[idx] = wrap(v)

    def __getitem__(self, item):
        if isinstance(item, basestring):
            if item.isdigit():
                return super(QueryList, self).__getitem__(int(item))

            left, sep, right = item.partition('.')
            return super(QueryList, self).__getitem__(left)[right]

        return super(QueryList, self).__getitem__(item)

    def __setitem__(self, key, value):
        value = wrap(value)

        if isinstance(key, basestring):
            if key.isdigit():
                super(QueryList, self).__setitem__(int(key), value)

            left, sep, right = key.partition('.')
            self[left][right] = value

        super(QueryList, self).__setitem__(key, value)

    def get(self, key, d=None):
        if isinstance(key, basestring):
            key = int(key)

        return self[key] if len(self) > key else d

    def query(self, *rules, **params):
        single = params.pop('single', False)
        count = params.pop('count', None)
        offset = params.pop('offset', None)
        limit = params.pop('limit', None)
        sort = params.pop('sort', None)
        postprocess = params.pop('callback', None)
        result = []

        if len(rules) == 0:
            result = list(self)
        else:
            for i in self:
                if matches(i, *rules):
                    result.append(i)

        if sort:
            def sort_transform(result, key):
                reverse = False
                if key.startswith('-'):
                    key = key[1:]
                    reverse=True
                _sort.append((key, reverse))

            _sort = []
            if isinstance(sort, string_types):
                sort_transform(_sort, sort)
            elif isinstance(sort, (tuple, list)):
                for s in sort:
                    sort_transform(_sort, s)
            if _sort:
                for key, reverse in reversed(_sort):
                    result = sorted(result, key=lambda x: x[key], reverse=reverse)

        if offset:
            result = result[offset:]

        if limit:
            result = result[:limit]

        if not result and single:
            return None

        if postprocess:
            result = filter(postprocess, result)

        if single:
            return result[0]

        if count:
            return len(result)

        return result


class QueryDict(dict):
    def __init__(self, *args, **kwargs):
        super(QueryDict, self).__init__(*args, **kwargs)
        for k, v in self.items():
            if isinstance(k, basestring):
                k = k.replace('.', r'\.')

            self[k] = wrap(v)

    def __getitem__(self, item):
        if not isinstance(item, basestring):
            return super(QueryDict, self).__getitem__(item)

        if super(QueryDict, self).__contains__(item):
            return super(QueryDict, self).__getitem__(item)

        left, right = partition(item)

        if not right:
            return super(QueryDict, self).__getitem__(left)

        return super(QueryDict, self).__getitem__(left)[right]

    def __setitem__(self, key, value):
        value = wrap(value)

        if not isinstance(key, basestring):
            return super(QueryDict, self).__setitem__(key, value)

        left, right = partition(key)

        if not right:
            return super(QueryDict, self).__setitem__(left, value)

        self[left][right] = value

    def __contains__(self, item):
        if not isinstance(item, basestring):
            return super(QueryDict, self).__contains__(item)

        left, right = partition(item)

        if not right:
            return super(QueryDict, self).__contains__(item)

        tmp = self.get(left)
        if not tmp:
            return False

        return right in tmp

    def get(self, k, d=None):
        return self[k] if k in self else d

    def set(self, key, value):
        value = wrap(value)

        if not isinstance(key, basestring):
            return super(QueryDict, self).__setitem__(key, value)

        left, right = partition(key)

        if not right:
            return super(QueryDict, self).__setitem__(left, value)

        if left not in self:
            self[left] = {}

        self[left].set(right, value)
