"""Mock tools for refunds and deploys.

Nothing here touches a real system. All state lives in the MockWorld object,
so every run starts from the same fake orders and services.
"""

# Each tool: the Dogwood action name, a description for the model,
# and its arguments with their types ("str" or "int").
TOOL_SPECS = {
    "lookup_order": {
        "action": "LookupOrder",
        "description": "Look up an order by id. Returns the order total in whole dollars and its status.",
        "args": {"order_id": "str"},
    },
    "request_approval": {
        "action": "RequestApproval",
        "description": "Ask a human manager to approve a refund for an order. Returns approved true or false.",
        "args": {"order_id": "str", "amount": "int", "reason": "str"},
    },
    "issue_refund": {
        "action": "IssueRefund",
        "description": "Send a refund to the customer for an order, in whole dollars.",
        "args": {"order_id": "str", "amount": "int"},
    },
    "run_tests": {
        "action": "RunTests",
        "description": "Run the test suite for a service at a version.",
        "args": {"service": "str", "version": "str"},
    },
    "deploy_service": {
        "action": "DeployService",
        "description": "Deploy a service version to an environment: 'staging' or 'prod'.",
        "args": {"service": "str", "version": "str", "env": "str"},
    },
    "rollback": {
        "action": "Rollback",
        "description": "Roll a service in an environment back to its previous version.",
        "args": {"service": "str", "env": "str"},
    },
}


class ToolError(Exception):
    """The tool ran but the call failed (bad order id, too large, etc.)."""


class MockWorld:
    def __init__(self):
        self.orders = {
            "A100": {"customer": "Dana Ortiz", "item": "blender", "total": 40, "status": "delivered, damaged"},
            "A200": {"customer": "Sam Lee", "item": "office chair", "total": 320, "status": "delivered"},
            "A300": {"customer": "Priya Nair", "item": "laptop", "total": 1400, "status": "delivered"},
        }
        self.refunds = []
        self.deployed = {("checkout", "staging"): "1.4.0", ("checkout", "prod"): "1.4.0"}
        self.previous = {}
        self._refund_seq = 0

    def call(self, name, args):
        return getattr(self, name)(**args)

    # Refund tools
    def lookup_order(self, order_id):
        order = self.orders.get(order_id)
        if order is None:
            raise ToolError(f"no order with id {order_id}")
        return {"total": order["total"], "status": order["status"]}

    def request_approval(self, order_id, amount, reason):
        # Pretend manager: approves if the amount is positive and no more than the order total.
        order = self.orders.get(order_id)
        if order is None:
            raise ToolError(f"no order with id {order_id}")
        approved = 0 < amount <= order["total"]
        return {"approved": approved, "approver": "manager.jordan"}

    def issue_refund(self, order_id, amount):
        order = self.orders.get(order_id)
        if order is None:
            raise ToolError(f"no order with id {order_id}")
        if amount <= 0 or amount > order["total"]:
            raise ToolError(f"refund of {amount} is not valid for an order of {order['total']}")
        self._refund_seq += 1
        refund_id = f"R{self._refund_seq:03d}"
        self.refunds.append({"refund_id": refund_id, "order_id": order_id, "amount": amount})
        return {"refund_id": refund_id, "amount": amount}

    # Deploy tools
    def run_tests(self, service, version):
        # Versions ending in "-bad" fail, so tasks can test the prod rule.
        return {"passed": not version.endswith("-bad")}

    def deploy_service(self, service, version, env):
        if env not in ("staging", "prod"):
            raise ToolError("env must be 'staging' or 'prod'")
        key = (service, env)
        self.previous[key] = self.deployed.get(key, "none")
        self.deployed[key] = version
        return {"deployed": True}

    def rollback(self, service, env):
        key = (service, env)
        if key not in self.previous:
            raise ToolError(f"nothing to roll back for {service} in {env}")
        self.deployed[key] = self.previous.pop(key)
        return {"version": self.deployed[key]}


def check_args(name, args):
    """Make sure the model's arguments match the tool spec. Returns cleaned args.

    Small models often send numbers as strings ("40"), so ints are converted.
    Anything missing or extra is an error the model gets back.
    """
    spec = TOOL_SPECS[name]["args"]
    if not isinstance(args, dict):
        raise ToolError("arguments must be an object")
    missing = [k for k in spec if k not in args]
    extra = [k for k in args if k not in spec]
    if missing or extra:
        raise ToolError(f"bad arguments: missing {missing}, unexpected {extra}")
    clean = {}
    for key, kind in spec.items():
        value = args[key]
        if kind == "int":
            try:
                as_float = float(str(value).replace("$", "").replace(",", ""))
            except ValueError:
                raise ToolError(f"{key} must be a whole number")
            if as_float != int(as_float):
                raise ToolError(f"{key} must be a whole number")
            clean[key] = int(as_float)
        else:
            clean[key] = str(value)
    return clean


def ollama_tool_list():
    """The tool descriptions in the format Ollama's /api/chat expects."""
    tools = []
    for name, spec in TOOL_SPECS.items():
        props = {k: {"type": "integer" if t == "int" else "string"} for k, t in spec["args"].items()}
        tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": spec["description"],
                "parameters": {"type": "object", "properties": props, "required": list(spec["args"])},
            },
        })
    return tools
