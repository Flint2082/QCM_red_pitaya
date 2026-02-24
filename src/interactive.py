import src.QCM_package.QCM_interface as QCM_interface 
from IPython import start_ipython

rp_ip = "132.229.46.164"

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


