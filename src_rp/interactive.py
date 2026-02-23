import src_rp.packages.QCM_interface as QCM_interface 
from IPython import embed

rp_ip = "132.229.46.164"

qcm = QCM_interface.QCMInterface(rp_ip)

qcm.startup()

embed()
