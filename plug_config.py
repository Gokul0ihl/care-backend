from plugs.manager import PlugManager

# The former care_emergency plug is the in-repo hmis/ app (in
# LOCAL_APPS). Add any future plugs here.
plugs = []

manager = PlugManager(plugs)
