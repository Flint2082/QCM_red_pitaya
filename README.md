# QCM_red_pitaya

FPGA code for a quartz crystal microbalance measurement system. 
Designed to run on the Red Pitaya FPGA.
made using the CASPER toolflow.

## Block diagram

<img width="940" height="690" alt="image" src="https://github.com/user-attachments/assets/5986d6df-943e-458a-be10-527f9ac0927c" />



## Algorithm 
The system measures the frequency shifts of two modes of a quartz crystal microbalance (QCM) sensor: a temperature sensitive mode and a mass sensitive mode. The frequency shifts are modeled as polynomial functions of temperature change and mass change, represented by the following equations:
```math
    \begin{aligned}
        \Delta f_T &= \lambda_{T_3} \cdot \Delta T^3 + \lambda_{T_2} \cdot \Delta T^2 + \lambda_{T_1} \cdot \Delta T + \lambda_{T_0} \cdot \Delta m \\
        \Delta f_M &= \lambda_{M_3} \cdot \Delta T^3 + \lambda_{M_2} \cdot \Delta T^2 + \lambda_{M_1} \cdot \Delta T + \lambda_{M_0} \cdot \Delta m 
    \end{aligned}
```
Where:
-  $` \Delta f_T `$ : Frequency shift of the temperature sensitive mode
-  $` \Delta f_M `$ : Frequency shift of the mass sensitive mode
-  $` \Delta T `$ : Temperature change
-  $` \Delta m `$ : Mass change
-  $` \lambda_{T_i} `$ : Temperature coefficients for the temperature sensitive mode
-  $` \lambda_{M_i} `$ : Temperature coefficients for the mass sensitive mode

To isolate the temperature change \( \Delta T \), we eliminate the mass change \( \Delta m \) from the equations:
```math
    (\lambda_{M_3} \cdot \lambda_{T_0} - \lambda_{T_3} \cdot \lambda_{M_0}) \cdot \Delta T^3 +
    (\lambda_{M_2} \cdot \lambda_{T_0} - \lambda_{T_2} \cdot \lambda_{M_0}) \cdot \Delta T^2 + \\
    (\lambda_{M_1} \cdot \lambda_{T_0} - \lambda_{T_1} \cdot \lambda_{M_0}) \cdot \Delta T +
    \lambda_{M_0} \cdot \Delta f_T - \lambda_{T_0} \cdot \Delta f_M = 0
```
Then once \( \Delta T \) is determined, we can substitute it back into one of the original equations to find the mass change \( \Delta m \):
```math
    \Delta m = \frac{\Delta f_M - (\lambda_{M_3} \cdot \Delta T^3 + \lambda_{M_2} \cdot \Delta T^2 + \lambda_{M_1} \cdot \Delta T)}{\lambda_{M_0}}    
```
