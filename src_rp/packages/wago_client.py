from opcua import Client, ua
import enum

class WagoClient:
    def __init__(self, url="opc.tcp://192.168.1.50:4840", user="admin", password="wago"):#192.168.1.50
        self.url = url
        self.user = user
        self.password = password
        self.client = None
        self.batch_read_parameters = None
        self.base_node_id = None
        self.connect()

    def connect(self):
        try:
            self.client = Client(self.url)
            self.client.set_user(self.user)
            self.client.set_password(self.password)
            self.client.application_uri = "urn:wago-client"
            #self.client.set_timeout(500)
            self.client.connect()

            # 💡 Set socket timeout to 1000ms (1s)
            #self.client.uaclient.set_timeout(1000)

            print("[WAGO] Connected to OPC UA server")
        except Exception as e:
            print(f"[WAGO] Connection failed: {e}")
            self.client = None

    def disconnect(self):
        try:
            if self.client:
                self.client.disconnect()
                print("[WAGO] Disconnected")
        except Exception as e:
            print(f"[WAGO] Disconnect failed: {e}")

    def get_batch_write_parameters(self, node_ids):
        write_values = []
        for node_id in node_ids:
            wv = ua.WriteValue()
            wv.NodeId = ua.NodeId.from_string(node_id)
            wv.AttributeId = ua.AttributeIds.Value
            wv.Value = ua.DataValue(ua.Variant(0))  # Default; will be overwritten
            write_values.append(wv)
        return write_values

    def get_batch_read_parameters(self, node_ids):
        read_ids = []
        for nid in node_ids:
            rv = ua.ReadValueId()
            rv.NodeId = ua.NodeId.from_string(nid)
            rv.AttributeId = ua.AttributeIds.Value
            read_ids.append(rv)

        # Create a ReadParameters object
        read_params = ua.ReadParameters()
        read_params.NodesToRead = read_ids
        read_params.TimestampsToReturn = ua.TimestampsToReturn.Neither
        return read_params

    def batch_write(self, values, nodes):
        write_values = []

        for value, node in zip(values, nodes):
            write_value = ua.WriteValue()
            write_value.NodeId = node.nodeid
            write_value.AttributeId = ua.AttributeIds.Value

            # Infer the correct data type from the node or value
            if isinstance(value, bool):
                variant = ua.Variant(value, ua.VariantType.Boolean)
            elif isinstance(value, int):
                variant = ua.Variant(value, ua.VariantType.Int32)  # or Int16, UInt32 etc.
            elif isinstance(value, float):
                variant = ua.Variant(value, ua.VariantType.Float)
            elif isinstance(value, str):
                variant = ua.Variant(value, ua.VariantType.String)
            elif hasattr(value, "value"):  # for enum.IntEnum
                variant = ua.Variant(value.value, ua.VariantType.Int32)
            else:
                print(f"[WAGO] Warning: Unhandled type {type(value)}, using default Variant")
                variant = ua.Variant(value)

            data_value = ua.DataValue(variant)
            write_value.Value = data_value
            write_values.append(write_value)

        try:
            self.client.uaclient.write(write_values)
        except Exception as e:
            print(f"Batch write failed: {e}")



    def batch_read(self, read_parameters):
        try:
            batch_data = self.client.uaclient.read(read_parameters)
        except Exception as e:
            print(e)
            batch_data = None
        return batch_data

    def get_node(self, node_id: str):
        try:
            return self.client.get_node(node_id)
        except Exception as e:
            print(f"[WAGO] Get node failed: {e}")
            return None

    def has_node(self, key):
        try:
            node_id = f"ns=4;s=|var|750-8000 Basic Controller 100 2ETH ECO.Application.GVL_OPCUA.{key}"
            node = self.client.get_node(node_id)
            _ = node.get_value()  # try reading value to ensure existence
            return True
        except:
            return False

    def read_node(self, node):
        try:
            value = node.get_value()
            return value
        except Exception as e:
            print(f"[WAGO] Read failed for {e}")
            return None

    def write_node(self, node, value):
        try:
            variant = self._to_variant(value)
            node.set_value(variant)
        except Exception as e:
            print(f"[WAGO] Write failed for {e}")

    def _to_variant(self, value):
        if isinstance(value, enum.Enum):
            # Convert enum to integer with appropriate VariantType (e.g., Int32)
            return ua.Variant(value.value, ua.VariantType.Int32)
        # Other types...
        # e.g. int, float, bool, str handled normally:
        if isinstance(value, int):
            return ua.Variant(value, ua.VariantType.Int32)
        if isinstance(value, float):
            return ua.Variant(value, ua.VariantType.Float)
        if isinstance(value, bool):
            return ua.Variant(value, ua.VariantType.Boolean)
        if isinstance(value, str):
            return ua.Variant(value, ua.VariantType.String)

        # fallback: try default
        return ua.Variant(value)
