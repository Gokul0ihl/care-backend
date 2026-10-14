from plugs.manager import PlugManager
from plugs.plug import Plug

care_emergency = Plug(
    name="care_emergency",
    package_name="git+https://github.com/anupamkris-ihl/care_emergency.git",
    version="@v0.10.1",
    configs={},
)

plugs = [care_emergency]

manager = PlugManager(plugs)
