import json
import time

from persistent import Persistent
from ZODB.POSException import ConflictError

from pyramid.traversal import find_root
from pyramid.compat import is_nonstr_iter

from substanced.util import acquire

class LayerFull(Exception):
    pass

class Layer(object):
    """ Append-only list with maximum length.

    - Raise `LayerFull` on attempts to exceed that length.

    - Iteration occurs in reverse order of appends, and yields (index, object)
      tuples.

    - Hold generation (a sequence number) on behalf of `AppendStack`.
    """

    def __init__(self, max_length=100, generation=0):
        self._stack = []
        self._max_length = max_length
        self._generation = generation

    def __iter__(self):
        at = len(self._stack)
        while at > 0:
            at = at - 1
            yield at, self._stack[at]

    def newer(self, latest_index):
        """ Yield items appended after `latest_index`.
        
        Implemented as a method on the layer to work around lack of generator
        expressions in Python 2.5.x.
        """
        for index, obj in self:
            if index <= latest_index:
                break
            yield index, obj


    def push(self, obj):
        if len(self._stack) >= self._max_length:
            raise LayerFull()
        self._stack.append(obj)

class AppendStack(Persistent):
    """ Append-only stack w/ garbage collection.

    - Append items to most recent layer until full;  then add a new layer.
    
    - Discard "oldest" layer starting a new one.

    - Invariant:  the sequence of (generation, id) increases monotonically.

    - Iteration occurs in reverse order of appends, and yields
      (generation, index, object) tuples.
    """

    def __init__(self, max_layers=10, max_length=100):
        self._max_layers = max_layers
        self._max_length = max_length
        self._layers = [Layer(max_length, generation=0)]

    def __iter__(self):
        for layer in self._layers:
            for index, item in layer:
                yield layer._generation, index, item

    def newer(self, latest_gen, latest_index):
        for gen, index, obj in self:
            if (gen, index) <= (latest_gen, latest_index):
                break
            yield gen, index, obj

    def push(self, obj, pruner=None):
        layers = self._layers
        max = self._max_layers
        try:
            layers[0].push(obj)
        except LayerFull:
            new_layer = Layer(self._max_length,
                              generation=layers[0]._generation+1)
            new_layer.push(obj)
            self._layers.insert(0, new_layer)
        self._layers, pruned = layers[:max], layers[max:]
        if pruner is not None:
            for layer in pruned:
                pruner(layer._generation, layer._stack)

    def __getstate__(self):
        layers = [(x._generation, x._stack) for x in self._layers]
        return (self._max_layers, self._max_length, layers)

    def __setstate__(self, state):
        self._max_layers, self._max_length, layer_data = state
        self._layers = []
        for generation, items in layer_data:
            layer = Layer(self._max_length, generation)
            for item in items:
                layer.push(item)
            self._layers.append(layer)

    #
    # ZODB Conflict resolution
    #
    # The overall approach here is to compute the 'delta' from old -> new
    # (objects added in new, not present in old), and push them onto the
    # committed state to create a merged state.
    # Unresolvable errors include:
    # - any difference between O <-> C <-> N on the non-layers attributes.
    # - either C or N has its oldest layer in a later generation than O's
    #   newest layer.
    # Compute the O -> N diff via the following:
    # - Find the layer, N' in N whose generation matches the newest generation
    #   in O, O'.
    # - Compute the new items in N` by slicing it using the len(O').
    # - That slice, plus any newer layers in N, form the set to be pushed
    #   onto C.
    #   
    def _p_resolveConflict(self, old, committed, new):
        o_m_layers, o_m_length, o_layers = old
        c_m_layers, c_m_length, c_layers = committed
        m_layers = [x[:] for x in c_layers]
        n_m_layers, n_m_length, n_layers = new
        
        if not o_m_layers == n_m_layers == n_m_layers:
            raise ConflictError('Conflicting max layers')

        if not o_m_length == c_m_length == n_m_length:
            raise ConflictError('Conflicting max length')

        o_latest_gen = o_layers[0][0]
        o_latest_items = o_layers[0][1]
        c_earliest_gen = c_layers[-1][0]
        n_earliest_gen = n_layers[-1][0]

        if o_latest_gen < c_earliest_gen:
            raise ConflictError('Committed obsoletes old')

        if o_latest_gen < n_earliest_gen:
            raise ConflictError('New obsoletes old')

        new_objects = []
        for n_generation, n_items in n_layers:
            if n_generation > o_latest_gen:
                new_objects[:0] = n_items
            elif n_generation == o_latest_gen:
                new_objects[:0] = n_items[len(o_latest_items):]
            else:
                break

        while new_objects:
            to_push, new_objects = new_objects[0], new_objects[1:]
            if len(m_layers[0][1]) == c_m_length:
                m_layers.insert(0, (m_layers[0][0]+1, []))
            m_layers[0][1].append(to_push)

        return c_m_layers, c_m_length, m_layers[:c_m_layers]

class AuditScribe(object):
    def __init__(self, context):
        self.context = context

    def get_auditlog(self):
        return acquire(self.context, '__auditlog__', None)

    def add(self, name, oid, **kw):
        auditlog = self.get_auditlog()
        if auditlog is None:
            auditlog = AuditLog()
            setattr(find_root(self.context), '__auditlog__', auditlog)
        auditlog.add(name, oid, **kw)
            
    def newer(self, gen, idx, oids=None):
        auditlog = self.get_auditlog()
        if auditlog is None:
            return []
        return auditlog.newer(gen, idx, oids)

    def latest_id(self):
        auditlog = self.get_auditlog()
        if auditlog is None:
            return 0, 0
        gen, idx = auditlog.latest_id()
        return gen, idx
                
class AuditLogEntry(Persistent):
    def __init__(self, name, oid, payload, timestamp):
        self.name = name
        self.oid = oid
        self.payload = payload
        self.timestamp = timestamp

class AuditLog(Persistent):
    def __init__(self, max_layers=10, layer_size=100, entries=None):
        if entries is None: # for testing
            entries = AppendStack(max_layers, layer_size)
        self.entries = entries
    
    def add(self, name, oid, **kw):
        timestamp = time.time()
        payload = json.dumps(kw)
        entry = AuditLogEntry(name, oid, payload, timestamp)
        self.entries.push(entry)

    def newer(self, generation, index_id, oids=None):
        if oids and not is_nonstr_iter(oids):
            oids = [oids]
        items = self.entries.newer(generation, index_id)
        for gen, idx, entry in items:
            if (not oids) or entry.oid in oids:
                yield gen, idx, entry

    def latest_id(self):
        layers = self.entries._layers
        last_layer = layers[0]
        gen = last_layer._generation
        index_id = len(last_layer._stack)
        return gen, index_id

