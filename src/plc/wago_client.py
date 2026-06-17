# Responsible for:
#
# OPC UA transport to WAGO PLC
# Node ID resolution against the PLC namespace
# Batch read/write helpers

import enum
import threading

from opcua import Client, ua

# Namespace URL registered by the WAGO CoDeSys runtime (or the test server).
# If you don't know the URL, call client.get_namespace_array() after connecting
# and pass the matching index directly via the namespace_idx constructor parameter.
DEFAULT_NAMESPACE_URL = "urn:WAGO:UA:PlcRuntime"

# OPC-UA node path prefix shared by all QCM nodes.
# The GVL object name (e.g. GVL_QCM) is part of each individual key, not the base.
BASE_NODE_PATH = "|var|750-8000 Basic Controller 100 2ETH ECO.Application."


class WagoClient:
    def __init__(
        self,
        url: str = "opc.tcp://132.229.46.113:4840",
        user: str = "admin",
        password: str = "wago",
        namespace_url: str = DEFAULT_NAMESPACE_URL,
        namespace_idx: int | None = None,  # override URL lookup when index is known
        auto_connect: bool = True,
    ):
        self.url = url
        self.user = user
        self.password = password
        self.namespace_url = namespace_url
        self._ns_idx_override = namespace_idx  # skip URL lookup when set
        self.client: Client | None = None
        self._ns_idx: int | None = namespace_idx  # pre-seed cache if override given
        self._lock = threading.Lock()

        if auto_connect:
            self.connect()

    # --------------------------------------------------
    # Connection management
    # --------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self.client is not None

    def set_connection(self, url: str, user: str = "", password: str = ""):
        """Update connection parameters and drop the current connection.
        The worker's reconnect loop will re-establish with the new settings."""
        self.url = url
        self.user = user
        self.password = password
        self.disconnect()

    def connect(self) -> bool:
        c = None
        try:
            c = Client(self.url)
            # Only send credentials if provided — test servers often have no user manager
            if self.user:
                c.set_user(self.user)
            if self.password:
                c.set_password(self.password)
            c.application_uri = "urn:wago-client"
            c.connect()
            self.client = c
            self._ns_idx = self._ns_idx_override  # reset to override (or None for URL lookup)
            print("[WAGO] Connected to OPC UA server")
            return True
        except Exception as e:
            print(f"[WAGO] Connection failed: {e}")
            # connect() may fail after threads/socket were partially started —
            # tear the half-open client down so nothing is left running.
            if c is not None:
                try:
                    c.disconnect()
                except Exception:
                    pass
            self.client = None
            return False

    def disconnect(self):
        try:
            if self.client:
                self.client.disconnect()
                print("[WAGO] Disconnected")
        except Exception as e:
            print(f"[WAGO] Disconnect error: {e}")
        finally:
            self.client = None
            self._ns_idx = self._ns_idx_override

    def reconnect(self) -> bool:
        self.disconnect()
        return self.connect()

    def _drop_connection(self):
        """Tear down the client after an I/O error.

        Critically this calls the library's disconnect() instead of merely
        nulling our reference. Orphaning the Client leaves its internal threads
        running against the dead socket: the KeepAlive thread keeps calling
        open_secure_channel(renew=True) and crashes with an uncaught
        BrokenPipeError, and pending subscription-delete callbacks raise
        CancelledError. disconnect() stops (and joins) the keepalive thread
        before any network I/O, so those threads shut down cleanly."""
        client = self.client
        self.client = None
        self._ns_idx = self._ns_idx_override
        if client is None:
            return
        try:
            client.disconnect()
        except Exception:
            pass  # socket already dead — we only care that the threads stop

    # --------------------------------------------------
    # Node ID helpers
    # --------------------------------------------------

    def _get_ns_idx(self) -> int | None:
        """Resolve and cache the PLC namespace index."""
        if self._ns_idx is not None:
            return self._ns_idx
        try:
            self._ns_idx = self.client.get_namespace_index(self.namespace_url)
        except Exception as e:
            print(f"[WAGO] Namespace lookup failed: {e}")
            return None
        return self._ns_idx

    def build_node_id(self, key: str) -> str | None:
        """Build a full OPC-UA node ID string from a QCM key name."""
        idx = self._get_ns_idx()
        if idx is None:
            return None
        return f"ns={idx};s={BASE_NODE_PATH}{key}"

    # --------------------------------------------------
    # Key-based read / write (convenient, one node at a time)
    # --------------------------------------------------

    def read_by_key(self, key: str):
        if not self.is_connected:
            return None
        node_id = self.build_node_id(key)
        if node_id is None:
            return None
        try:
            return self.client.get_node(node_id).get_value()
        except Exception as e:
            print(f"[WAGO] Read failed for '{key}': {e}")
            self._drop_connection()
            return None

    def write_by_key(self, key: str, value) -> bool:
        if not self.is_connected:
            return False
        node_id = self.build_node_id(key)
        if node_id is None:
            return False
        try:
            self.client.get_node(node_id).set_value(self._to_variant(value))
            return True
        except Exception as e:
            print(f"[WAGO] Write failed for '{key}': {e}")
            self._drop_connection()
            return False

    # --------------------------------------------------
    # Batch key-based read / write (one OPC-UA round-trip)
    # --------------------------------------------------

    def batch_read_by_keys(self, keys: list[str]) -> dict | None:
        """Read multiple keys in one request. Returns {key: value} or None on error."""
        if not self.is_connected:
            return None

        read_ids, valid_keys = [], []
        for key in keys:
            node_id = self.build_node_id(key)
            if node_id is None:
                continue
            rv = ua.ReadValueId()
            rv.NodeId = ua.NodeId.from_string(node_id)
            rv.AttributeId = ua.AttributeIds.Value
            read_ids.append(rv)
            valid_keys.append(key)

        if not read_ids:
            return None

        params = ua.ReadParameters()
        params.NodesToRead = read_ids
        params.TimestampsToReturn = ua.TimestampsToReturn.Neither

        try:
            results = self.client.uaclient.read(params)
            return {
                k: (r.Value.Value if r.Value is not None else None)
                for k, r in zip(valid_keys, results)
            }
        except Exception as e:
            print(f"[WAGO] Batch read failed: {e}")
            self._drop_connection()
            return None

    def batch_write_by_keys(self, kv: dict[str, object]) -> bool:
        """Write multiple key-value pairs in one request."""
        if not self.is_connected:
            return False

        write_values = []
        for key, value in kv.items():
            node_id = self.build_node_id(key)
            if node_id is None:
                continue
            wv = ua.WriteValue()
            wv.NodeId = ua.NodeId.from_string(node_id)
            wv.AttributeId = ua.AttributeIds.Value
            wv.Value = ua.DataValue(self._to_variant(value))
            write_values.append(wv)

        if not write_values:
            return False

        try:
            params = ua.WriteParameters()
            params.NodesToWrite = write_values
            self.client.uaclient.write(params)
            return True
        except Exception as e:
            print(f"[WAGO] Batch write failed: {e}")
            self._drop_connection()
            return False

    # --------------------------------------------------
    # Legacy low-level API (kept for backward compatibility)
    # --------------------------------------------------

    def get_batch_write_parameters(self, node_ids):
        write_values = []
        for node_id in node_ids:
            wv = ua.WriteValue()
            wv.NodeId = ua.NodeId.from_string(node_id)
            wv.AttributeId = ua.AttributeIds.Value
            wv.Value = ua.DataValue(ua.Variant(0))
            write_values.append(wv)
        return write_values

    def get_batch_read_parameters(self, node_ids):
        read_ids = []
        for nid in node_ids:
            rv = ua.ReadValueId()
            rv.NodeId = ua.NodeId.from_string(nid)
            rv.AttributeId = ua.AttributeIds.Value
            read_ids.append(rv)
        params = ua.ReadParameters()
        params.NodesToRead = read_ids
        params.TimestampsToReturn = ua.TimestampsToReturn.Neither
        return params

    def batch_write(self, values, nodes):
        write_values = []
        for value, node in zip(values, nodes):
            wv = ua.WriteValue()
            wv.NodeId = node.nodeid
            wv.AttributeId = ua.AttributeIds.Value
            wv.Value = ua.DataValue(self._to_variant(value))
            write_values.append(wv)
        try:
            params = ua.WriteParameters()
            params.NodesToWrite = write_values
            self.client.uaclient.write(params)
        except Exception as e:
            print(f"[WAGO] Batch write failed: {e}")

    def batch_read(self, read_parameters):
        try:
            return self.client.uaclient.read(read_parameters)
        except Exception as e:
            print(f"[WAGO] Batch read failed: {e}")
            return None

    def get_node(self, node_id: str):
        try:
            return self.client.get_node(node_id)
        except Exception as e:
            print(f"[WAGO] Get node failed: {e}")
            return None

    def has_node(self, url, node_id_base, key) -> bool:
        try:
            idx = self.client.get_namespace_index(url)
            node = self.client.get_node(f"ns={idx};s={node_id_base}{key}")
            _ = node.get_value()
            return True
        except Exception:
            return False

    def read_node(self, node):
        try:
            return node.get_value()
        except Exception as e:
            print(f"[WAGO] Read node failed: {e}")
            return None

    def write_node(self, node, value):
        try:
            node.set_value(self._to_variant(value))
        except Exception as e:
            print(f"[WAGO] Write node failed: {e}")

    def _to_variant(self, value) -> ua.Variant:
        # bool must be checked before int (bool is a subclass of int)
        if isinstance(value, bool):
            return ua.Variant(value, ua.VariantType.Boolean)
        if isinstance(value, enum.Enum):
            return ua.Variant(value.value, ua.VariantType.Int32)
        if isinstance(value, int):
            return ua.Variant(value, ua.VariantType.Int32)
        if isinstance(value, float):
            return ua.Variant(value, ua.VariantType.Float)
        if isinstance(value, str):
            return ua.Variant(value, ua.VariantType.String)
        return ua.Variant(value)
