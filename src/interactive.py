import core.QCM_interface as QCM_interface
from IPython import start_ipython
import sys



# rp_ip = "132.229.46.164"
# rp_ip = "rp-f0ea58.local"
#rp_ip = "192.168.1.55"

if len(sys.argv) > 1:
    rp_ip = sys.argv[1]
else:
    quit("Usage: python interactive.py <rp_ip_address>")


qcm = QCM_interface.QCMInterface(rp_ip)

qcm.startup()

ns = globals()
ns.update(
    {
        name: getattr(qcm, name)
        for name in dir(qcm)
        if not name.startswith("_")
    }
)

start_ipython(argv=[], user_ns=ns)


