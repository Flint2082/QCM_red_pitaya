import csv
import math
import numpy as np


# A root counts as real when its imaginary part is within this of zero, as
# np.isclose(roots.imag, 0) decided it when the solves used np.roots.
_REAL_ROOT_TOL = 1e-8


def _real_quadratic_roots(a, b, c):
    """Real roots of a*x^2 + b*x + c; a zero leading coefficient drops the degree."""
    if a == 0:
        return [] if b == 0 else [-c / b]
    disc = b * b - 4 * a * c
    if disc < 0:
        # A near-double root, split off the real axis by rounding.
        return [-b / (2 * a)] if math.sqrt(-disc) / (2 * abs(a)) <= _REAL_ROOT_TOL else []
    q = -0.5 * (b + math.copysign(math.sqrt(disc), b))
    if q == 0:
        return [0.0, 0.0]
    return [q / a, c / q]


def real_cubic_roots(a, b, c, d, guess=0.0):
    """Real roots of a*x^3 + b*x^2 + c*x + d, the ones np.roots would give, in
    pure Python: Newton's method from `guess` finds one real root (a cubic
    always has one), and the quadratic left after dividing it out gives the
    others. np.roots takes the eigenvalues of the companion matrix, ~1.4 ms on
    the Pitaya's A9; this is tens of microseconds. Leading zero coefficients
    drop the degree, as np.roots does. Falls back to np.roots in the rare case
    that Newton does not settle (it can cycle)."""
    if a == 0:
        return _real_quadratic_roots(b, c, d)
    x = float(guess)
    for _ in range(60):
        slope = (3 * a * x + 2 * b) * x + c
        if slope == 0:
            break
        step = (((a * x + b) * x + c) * x + d) / slope
        x -= step
        if abs(step) <= 1e-12 * max(1.0, abs(x)):
            q = b + a * x
            return [x] + _real_quadratic_roots(a, q, c + q * x)
    roots = np.roots([a, b, c, d])
    return [float(r.real) for r in roots if abs(r.imag) <= _REAL_ROOT_TOL]

class TempCompAlgorithm:
    def __init__(self, coefficients, T_start, fT_start, fM_start,
                 mat_dens=19320,            # kg/m^3 = Material density (e.g. gold)
                 sens_area=5.25e-5,         # m^2 = Sensor area (e.g. 5.25e-5 m^2 for 8.2 mm dia apature)
                 mass_sensitivity=-13.3e-8, # kg/m^2/Hz = Mass sensitivity, negative: added mass lowers the frequency (-13.3 ng/(cm2*Hz) for ~6 MHz SC-cut, per Inficon)
                 z_ratio=1.0,               # acoustic impedance ratio quartz/film (gold 0.381); 1.0 ~ plain Sauerbrey
                 freq_virgin=None,          # Hz = pristine (uncoated) crystal frequency; None/0 = use fM_start
                 tooling_ratio=1.0          # proportional scaling of the reported thickness (sensor/substrate geometry); 1.0 = no scaling
                 ):
        # Calibration coefficients are provided directly (e.g. from the active
        # crystal profile).
        # so only the fM_1..3 / fT_1..3 terms are read from the supplied dict.
        self.mass_sensitivity = mass_sensitivity        
        self.fT_0 = (fT_start/fM_start) / (mass_sensitivity * sens_area)  # Hz/kg = Temp sensitivity NOTE: this is derived from the starting frequencies and mass sensitivity, not fitted from calibration data
        self.fM_0 = 1 / ( mass_sensitivity * sens_area )                  # Hz/kg = Mass sensitivity              
        self.fT_1 = coefficients['fT_1']               
        self.fT_2 = coefficients['fT_2']
        self.fT_3 = coefficients['fT_3']
        self.fM_1 = coefficients['fM_1']
        self.fM_2 = coefficients['fM_2']
        self.fM_3 = coefficients['fM_3']
        
        # values from the starting measurement
        self.T_start = T_start
        # Last physically-selected temperature; seeds the cubic-root selection so
        # the solve tracks the real temperature continuously (see FreqToTemp).
        self._last_T = T_start
        self.fT_start = fT_start
        self.fM_start = fM_start
        
        self.mat_dens = mat_dens        # kg/m^3 = Material density
        self.sens_area = sens_area      # m^2 = Sensor area
        self.z_ratio = z_ratio          # quartz/film acoustic impedance ratio
        self.f_virgin = freq_virgin or fM_start  # Hz = unloaded reference for the Z-match conversion
        self.tooling_ratio = tooling_ratio  # proportional scaling of the reported thickness
        
        # Coefficients for the cubic equation a*T_dif^3 + b*T_dif^2 + c*T_dif + d = 0
        # with d calculated later
        self.a = (self.fM_3*self.fT_0 - self.fT_3*self.fM_0)
        self.b = (self.fM_2*self.fT_0 - self.fT_2*self.fM_0)
        self.c = (self.fM_1*self.fT_0 - self.fT_1*self.fM_0)

    def _zmatch_areal_mass(self, f_loaded):
        """Z-match (Lu-Lewis) areal mass [kg/m^2] of the film between the virgin
        crystal frequency and f_loaded. The quartz constant Nq*rho_q is expressed
        through the crystal's calibrated mass sensitivity: Nq*rho_q = |ms| * f_v^2.
        Reduces to linear Sauerbrey for small loads and Z=1 (arctan(tan(x)) = x)."""
        Nq_rho_q = abs(self.mass_sensitivity) * self.f_virgin**2  # kg*Hz/m^2
        x = math.pi * (self.f_virgin - f_loaded) / self.f_virgin
        # math rather than numpy: four calls per sample, on scalars, where
        # numpy's per-call overhead is most of the cost.
        return (Nq_rho_q / (math.pi * self.z_ratio * f_loaded)) * math.atan(self.z_ratio * math.tan(x))

    def freq_to_thickness(self, f_from, f_to):
        """Film thickness [m] deposited while the (temperature-clean) mass-mode
        frequency moved from f_from to f_to, via the Z-match equation."""
        return (self._zmatch_areal_mass(f_to) - self._zmatch_areal_mass(f_from)) / self.mat_dens

    def FreqToThickness(self, fM):
        """Uncompensated thickness [nm] from the mass mode alone, for a run with
        no temperature mode: the whole frequency shift since the start is taken
        as film, so it is only as good as the crystal's temperature is steady.
        None on failure, like FreqToTemp, so a bad sample cannot stop the loop."""
        try:
            return self.freq_to_thickness(self.fM_start, fM) * 1e9 * self.tooling_ratio
        except Exception as e:
            print(f"Error in FreqToThickness: {e}")
            return None
        
        
         
    def FreqToTemp(self, fT, fM, hold_temp=False):
        """Solve for the temperature from the two mode frequencies and convert the
        mass mode to thickness.

        hold_temp keeps the last solved temperature instead of solving for a new
        one. It exists for a lost temp-mode lock: fT is then not a resonance, so
        solving would put a meaningless temperature into the compensation and
        corrupt the compensated thickness — the one number the deposition is
        controlled on. The mass mode is still measured normally, so freezing the
        temperature freezes only the thermal *correction* term; the compensated
        thickness keeps tracking the film. The correction goes stale at whatever
        rate the crystal's temperature actually drifts, which for a lost lock of
        a few seconds is far smaller than the error a garbage fT would inject.
        """
        try:
        
            # Difference from the starting value. The mass mode is measured
            # either way — only the temperature solve below depends on fT.
            fM_dif = fM - self.fM_start

            if hold_temp:
                # fT is untrustworthy; hold the temperature (and leave _last_T
                # alone, so a long hold does not drift on its own).
                T = self._last_T
            else:
                fT_dif = fT - self.fT_start

                # Calculate the 'd' components for the temperature and mass modes
                fT_d = self.fT_start - self.fT_3 * self.T_start**3 - self.fT_2 * self.T_start**2 - self.fT_1 * self.T_start
                fM_d = self.fM_start - self.fM_3 * self.T_start**3 - self.fM_2 * self.T_start**2 - self.fM_1 * self.T_start

                # d coefficient for the cubic equation a*T_dif^3 + b*T_dif^2 + c*T_dif + d = 0
                d = self.fM_0*(fT_dif-fT_d) - self.fT_0*(fM_dif - fM_d)

                # The real roots of the cubic (physical temperatures are real),
                # searched from the last temperature.
                real_roots = real_cubic_roots(self.a, self.b, self.c, d, guess=self._last_T)

                # The cubic can have up to three real roots but only one is physical.
                # Pick the root closest to the last known temperature (T_start on
                # the first sample). This keeps the solve on the correct, continuous
                # branch instead of latching onto a spurious root, which would
                # corrupt the temperature and the compensated thickness while
                # leaving the uncompensated value untouched.
                if not real_roots:
                    # No real solution — frequencies inconsistent with the calibration.
                    # Hold the previous temperature rather than crash the running loop.
                    print("FreqToTemp: no real root; holding last temperature")
                    T = self._last_T
                else:
                    T = min(real_roots, key=lambda r: abs(r - self._last_T))
                self._last_T = T

            # Calculate the compensated mass change using the found temperature
            M_dif = -(-fM_dif + (self.fM_3 * T**3 + self.fM_2 * T**2 + self.fM_1 * T) - (self.fM_3 * self.T_start**3 + self.fM_2 * self.T_start**2 + self.fM_1 * self.T_start)) / self.fM_0

            compensated_m_freq = self.fM_start + self.fM_0 * M_dif  # measured freq minus the thermal shift

            # frequency -> thickness via the Z-match (Lu-Lewis) equation; the raw
            # frequency gives the uncompensated value, the temperature-clean
            # compensated frequency gives the compensated one. The tooling ratio is
            # a simple proportional correction for the sensor/substrate geometry.
            uncompensated_thickness_nm = self.freq_to_thickness(self.fM_start, fM) * 1e9 * self.tooling_ratio
            compensated_thickness_nm = self.freq_to_thickness(self.fM_start, compensated_m_freq) * 1e9 * self.tooling_ratio

            return T, uncompensated_thickness_nm, compensated_thickness_nm, compensated_m_freq
        except Exception as e:
            print(f"Error in FreqToTemp: {e}")
            return None, None, None, None


# The crystal's absolute temperature is looked for in this range. Outside it the
# calibration cubics have extrapolated too far to be believed, and the mass-free
# combination below can turn over, giving a second root: for infi2439_SC_1 it
# does near -12 degC.
ABSOLUTE_T_RANGE = (-10.0, 120.0)


def absolute_temperature(coefficients, fM, fT, near=25.0):
    """The crystal's temperature (degC) from its two mode frequencies alone: no
    T start, no thermometer. None when the frequencies do not give one.

    FreqToTemp works from changes since the run start and needs T start for
    that. The calibration fit, though, gives both modes' whole curves,
    f(T) = f_0 + f_1*T + f_2*T^2 + f_3*T^3, so with the constants fM_0 and fT_0
    the frequencies say the temperature by themselves.

    fT alone would do on a clean crystal, but deposited film lowers fT too (by
    about fT/fM times what it lowers fM), and 1 nm of gold would read 1-1.5 degC
    hot. The combination fT - rho*fM is the mass-free one FreqToTemp solves, and
    with rho = fT_0/fM_0:
      - film shifts both modes in that ratio, so it cancels;
      - the constants cancel by the choice of rho, leaving
        fT - rho*fM = pT(T) - rho*pM(T), with pT, pM the cubics without constants;
      - a reference-clock error scales both modes alike. That scales the
        combination, which is only a few kHz, so it drops out as well.
    What does not cancel is a shift of one mode alone: 1 Hz on fM moves the
    answer by about 0.01 degC.

    The constants have to come from a calibration of this crystal; the defaults
    of a new profile describe a different one. near picks between roots if the
    range holds more than one."""
    try:
        fM_0, fT_0 = float(coefficients["fM_0"]), float(coefficients["fT_0"])
        fM, fT = float(fM), float(fT)
    except (KeyError, TypeError, ValueError):
        return None
    if not (fM_0 > 0 and fT_0 > 0 and fM > 0 and fT > 0 and np.isfinite(fM) and np.isfinite(fT)):
        return None
    rho = fT_0 / fM_0
    roots = real_cubic_roots(coefficients["fT_3"] - rho * coefficients["fM_3"],
                             coefficients["fT_2"] - rho * coefficients["fM_2"],
                             coefficients["fT_1"] - rho * coefficients["fM_1"],
                             -(fT - rho * fM), guess=near)
    lo, hi = ABSOLUTE_T_RANGE
    T = [r for r in roots if lo <= r <= hi]
    if not T:
        return None
    return float(min(T, key=lambda r: abs(r - near)))



def calculateCoeffecients(calibration_file, coeffecient_file):
    # matplotlib is imported here rather than at the top of the module because
    # this is the only thing in src/ that needs it, and it is an interactive
    # bench helper (it prompts on stdin and calls plt.show()) that the running
    # instrument never invokes. At module level the import made matplotlib a
    # hard install dependency of the measurement service — a big one, with no
    # wheel for the Pitaya's armv7 platform, so an offline install would have to
    # build it from source.
    import matplotlib.pyplot as plt

    
    with open(calibration_file, mode='r') as file:
        reader = csv.DictReader(file)
        calibration_data = [row for row in reader]
        
    with open(coeffecient_file, mode='r') as file:
        reader = csv.DictReader(file)
        old_coeffecients = {row['Name']: float(row['value']) for row in reader}
        
        
    mass_coef = np.polyfit(
        [float(row['Temp']) for row in calibration_data],
        [float(row['Freq_M']) for row in calibration_data],
        deg = 3
    )
    
    temp_coef = np.polyfit(
        [float(row['Temp']) for row in calibration_data],
        [float(row['Freq_T']) for row in calibration_data],
        deg = 3
    )
        
    
    fig, (ax1, ax2) = plt.subplots(2,1, sharex=True)
    x = np.linspace(20,100,100)
    
    ### MASS MODE
    ax1.set_title("Mass mode")
    ax1.set_xlabel("Temperature [C]")
    ax1.set_ylabel("Delta Frequency [Hz]")
    
    y = mass_coef[0]*x**3 + mass_coef[1]*x**2 +mass_coef[2]*x + mass_coef[3]
    ax1.plot(x,y, color='black', linestyle='solid')  ### plot polynomial
    
    y = old_coeffecients['fM_3']*x**3 + old_coeffecients['fM_2']*x**2 + old_coeffecients['fM_1']*x + old_coeffecients['fM_0']
    ax1.plot(x,y, color='grey', linestyle='dashdot')   ### plot old polynomial
    
    ax1.scatter(
        [float(row['Temp']) for row in calibration_data],
        [float(row['Freq_M']) for row in calibration_data],
        color = 'r',
        marker = 'x'
    )
    
                                                    
    
    
    ### TEMP MODE
    ax2.set_title("Temp mode")
    ax2.set_xlabel("Temperature [C]")
    ax2.set_ylabel("Delta Frequency [Hz]")
    
    y = temp_coef[0]*x**3 + temp_coef[1]*x**2 + temp_coef[2]*x + temp_coef[3]
    ax2.plot(x,y, color='black', linestyle='solid')   ### plot polynomial
    
    y = old_coeffecients['fT_3']*x**3 + old_coeffecients['fT_2']*x**2 + old_coeffecients['fT_1']*x + old_coeffecients['fT_0']
    ax2.plot(x,y, color='grey', linestyle='dashdot')   ### plot old polynomial
    
    ax2.scatter(
        [float(row['Temp']) for row in calibration_data],
        [float(row['Freq_T']) for row in calibration_data],
        color = 'r',
        marker = 'x'
    )
           
    fig.tight_layout() # adjust graph spacing
    plt.show()
    
    print("\n")
    print(f"fM_0: {mass_coef[3]}")
    print(f"fM_1: {mass_coef[2]}")
    print(f"fM_2: {mass_coef[1]}")
    print(f"fM_3: {mass_coef[0]}")
    print(f"fT_0: {temp_coef[3]}")
    print(f"fT_1: {temp_coef[2]}")
    print(f"fT_2: {temp_coef[1]}")
    print(f"fT_3: {temp_coef[0]}")
    
    # confirm overwrite
    if(input("Do you want to overwrite 'coeffecients.csv' (y/n): ") != 'y'):
        print("aborted")
        return
    
    with open(coeffecient_file, mode='w') as file:
        file.write("Name,value\n")
        file.write(f"fM_0,{mass_coef[3]}\n")
        file.write(f"fM_1,{mass_coef[2]}\n")
        file.write(f"fM_2,{mass_coef[1]}\n")
        file.write(f"fM_3,{mass_coef[0]}\n")
        file.write(f"fT_0,{temp_coef[3]}\n")
        file.write(f"fT_1,{temp_coef[2]}\n")
        file.write(f"fT_2,{temp_coef[1]}\n")
        file.write(f"fT_3,{temp_coef[0]}\n")

    
        
        
        
        
        
        
