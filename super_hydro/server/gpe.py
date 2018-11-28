import attr

import numpy as np
import numpy.fft
from .. import utils

try:
    import mmfutils.performance.fft
except ImportError:
    mmfutils = None

try:
    import numexpr
except ImportError:
    numexpr = None
    

@attr.s
class Dispersion(object):
    """Tools for computing porperties of the lower band dispersion.

    Everything is expressed in dimensionless units with $k/k_r$,
    $d=\delta/4E_R$, $w=\Omega/4E_R$, and $E_\pm/2E_R$.

    Examples
    --------
    >>> E = Dispersion(w=1.5/4, d=0.543/4)
    >>> ks = np.linspace(-3, 3, 500)
    >>> ks_ = (ks[1:] + ks[:-1])/2.0
    >>> ks__ = ks[1:-1]
    >>> Es = E(ks).T
    >>> dEs = E(ks, d=1).T
    >>> ddEs = E(ks, d=2).T
    >>> dEs_ = np.diff(Es, axis=0)/np.diff(ks)[:, None]
    >>> ddEs__ = np.diff(dEs_, axis=0)/np.diff(ks_)[:, None]
    >>> np.allclose((dEs[1:] + dEs[:-1])/2, dEs_, rtol=0.01)
    True
    >>> np.allclose(ddEs[1:-1], ddEs__, rtol=0.04)
    True
    """
    d = attr.ib()
    w = attr.ib()

    def Es(self, k, d=0):
        D = np.sqrt((k-self.d)**2 + self.w**2)
        if d == 0:
            res = (k**2 + 1)/2.0 - D, (k**2 + 1)/2.0 + D
        elif d == 1:
            res = k - (k-self.d)/D, k + (k-self.d)/D
        elif d == 2:
            res = 1.0 - self.w**2/D**3, 1.0 + self.w**2/D**3
        else:
            raise NotImplementedError("Only d=0, 1, or 2 supported. (got d={})"
                                      .format(d))
        return np.asarray(res)

    __call__ = Es

    def newton(self, k):
        return k - self.Es(k, d=1)[0]/self.Es(k, d=2)[0]

    def get_k0(self, N=5):
        """Return the minimum of the lower dispersion branch."""
        # Note that sign(k_0) = -sign(d) for the lower minimum
        k0 = -np.sign(self.d)/2.0
        for n in range(N):
            k0 = self.newton(k0)
        return k0


class State(object):
    """

    Parameters
    ----------
    V0_mu : float
       Potential strength in units of mu
    healing_length:
    """
    def __init__(self, Nxy=(32, 32), dx=1.0,
                 healing_length=10.0, r0=10.0, V0_mu=0.5,
                 cooling_phase=1.0+0.01j,
                 cooling_steps=100, dt_t_scale=0.1,
                 soc=False,
                 soc_d=0.05, soc_w=0.5,
                 winding=10,
                 cylinder=True,
                 test_finger=False,
                 N_tracer_particles=1000):
        g = hbar = m = 1.0
        self.g = g
        self.hbar = hbar
        self.m = m
        self.r0 = r0
        self.V0_mu = V0_mu
        self.Nxy = Nx, Ny = Nxy
        self.dx = dx
        self.Lxy = Lx, Ly = Lxy = np.asarray(Nxy)*dx
        self.healing_length = healing_length
        self.cooling_phase = cooling_phase
        dx, dy = np.divide(Lxy, Nxy)
        x = (np.arange(Nx)*dx - Lx/2.0)[:, None]
        y = (np.arange(Ny)*dy - Ly/2.0)[None, :]
        self.xy = (x, y)

        self.N_tracer_particles = N_tracer_particles
        kx = 2*np.pi * np.fft.fftfreq(Nx, dx)[:, None]
        ky = 2*np.pi * np.fft.fftfreq(Ny, dy)[None, :]
        self.kxy = (kx, ky)

        if soc:
            self._dispersion = Dispersion(d=soc_d, w=soc_w)
            kR = 3 / self.healing_length
            kR = 1.0/self.r0
            k0 = self._dispersion.get_k0()
            E0 = self._dispersion(k0)[0]
            kx2 = 2*kR**2 * (self._dispersion(kx/kR + k0) - E0)[0]
        else:
            kx2 = kx**2
        self._kx2 = kx2
        self.K = hbar**2*(kx2 + ky**2)/2.0/self.m

        self.n0 = n0 = hbar**2/2.0/healing_length**2/g
        self.mu = g*n0
        mu_min = max(0, min(self.mu, self.mu*(1-self.V0_mu)))
        self.c_s = np.sqrt(self.mu/self.m)
        self.c_min = np.sqrt(mu_min/self.m)
        #self.v_max = 1.1*self.c_min
        
        self.data = np.ones(Nxy, dtype=complex) * np.sqrt(n0)

        self.cylinder = cylinder
        self._V_trap = self.get_V_trap()

        if mmfutils and False:
            self.fft = mmfutils.performance.fft.get_fftn_pyfftw(self.data)
            self.ifft = mmfutils.performance.fft.get_ifftn_pyfftw(self.data)
        else:
            self.fft = np.fft.fftn
            self.ifft = np.fft.ifftn

        self._N = self.get_density().sum()

        self.test_finger = test_finger
        self.z_finger = 0 + 0j
        self.pot_k_m = 10.0
        self.pot_z = 0 + 0j
        self.pot_v = 0 + 0j
        self.pot_damp = 4.0

        self.t = -10000
        self.dt = dt_t_scale*self.t_scale
        self.cooling_phase = 1j
        self.step(cooling_steps)
        if cylinder:
            self.data *= np.exp(1j*winding*np.angle(x+1j*y))
        self.t = 0
        self.cooling_phase = cooling_phase
        self.dt = dt_t_scale*self.t_scale
        self.c_s = np.sqrt(self.mu/self.m)

        self._par_pos = self.tracer_part_create()


    @property
    def _phase(self):
        return -1j/self.hbar/self.cooling_phase

    def get_v_max(self, density):
        #c_min = 1.0*np.sqrt(self.g*density.min()/self.m)
        c_mean = 1.0*np.sqrt(self.g*density.mean()/self.m)
        return c_mean

    def get_density(self):
        y = self.data
        return (y.conj()*y).real

    def set_xy0(self, x0, y0):
        self.z_finger = x0 + 1j*y0

    @property
    def t_scale(self):
        return self.hbar/self.K.max()

    @property
    def z_finger(self):
        if self.test_finger:
            if self.t >= 0:
                return 3.0*np.exp(1j*self.t/5)
            else:
                return 3.0
        else:
            return self._z_finger

    @z_finger.setter
    def z_finger(self, z_finger):
        self._z_finger = z_finger

    def tracer_part_create(self):
        N_particles = self.N_tracer_particles
        np.random.seed(1)
        Nx, Ny = self.Nxy
        x, y = self.xy
        x, y = np.ravel(x), np.ravel(y)
        n = self.get_density()
        n_max = n.max()

        particles = []
        while len(particles) < N_particles:            
            ix = np.random.randint(Nx)
            iy = np.random.randint(Ny)

            if np.random.random()*n_max <= n[ix, iy]:
                particles.append(x[ix] + 1j*y[iy])
        return (np.asarray(particles))

    def get_inds(self, pos):
        """Return the indices (ix, iy) to the nearest point on the
        grid.
        
        """
        x, y = self.xy
        Lx, Ly = self.Lxy
        Nx, Ny = self.Nxy
        Npart = len(pos)
        pos = (pos + (Lx+1j*Ly)/2.0)
        ix = np.round((pos.real / Lx) * Nx).astype(int) % Nx
        iy = np.round((pos.imag / Ly) * Ny).astype(int) % Nx
        return (ix, iy)

    def update_tracer_velocity(self):
        """Define the velocity field for the particles"""
        px, py = self.kxy
        px *= self.hbar
        py *= self.hbar
        m = self.m
        n = self.data.conj()*self.data
		# self._data_fft == self.fft(self.data)
        v_x = (self.ifft(px*self.fft(self.data)) / self.data / m).real
        v_y = (self.ifft(py*self.fft(self.data)) / self.data / m).real
        self.v_trace = (v_x + 1j*v_y)
    
    def update_tracer_pos(self, dt):
        """Applies the velocity field to the particle positions and
        updates with time dt""" 
        if not hasattr(self, '_par_pos'):
            return
        if not hasattr(self, 'v_trace'):
            self.update_tracer_velocity()
        i = 0
        pos = self._par_pos
        ix, iy = self.get_inds(pos)
        v = self.v_trace[ix, iy]
        pos += dt*v

    def get_tracer_particles(self):
        """Return the tracer particle positions.

        This is a 1D complex array of the particle positions in data
        coordinates."""
        return self._par_pos

    def get_V_trap(self):
        """Return any static trapping potential."""
        if self.cylinder:
            x, y = self.xy
            Lx, Ly = self.Lxy
            r2_ = (2*x/Lx)**2 + (2*y/Ly)**2
            return 100*self.mu*utils.mstep(r2_ - 0.8, 0.2)
        else:
            return 0
        
    def get_Vext(self):
        """Return the full external potential."""
        x, y = self.xy
        x0, y0 = self.pot_z.real, self.pot_z.imag
        Lx, Ly = self.Lxy

        V = 0
        
        # Wrap displaced x and y in periodic box.
        x = (x - x0 + Lx/2) % Lx - Lx/2
        y = (y - y0 + Ly/2) % Ly - Ly/2
        r2 = x**2 + y**2
        return self._V_trap + self.V0_mu*self.mu * np.exp(-r2/2.0/self.r0**2)

    def apply_expK(self, dt, factor=1.0):
        y = self.data
        if numexpr:
            yt = self.fft(y)
            self.data[...] = self.ifft(
                numexpr.evaluate(
                    'exp(f*K)*y',
                    local_dict=dict(
                        f=self._phase*dt*factor,
                        K=self.K,
                        y=yt)))
        else:
            self.data[...] = self.ifft(np.exp(self._phase*dt*self.K*factor)
                                       * self.fft(y))

    def apply_expV(self, dt, factor=1.0, density=None):
        y = self.data
        if density is None:
            density = self.get_density()
        n = density
        if numexpr:
            self.data[...] = numexpr.evaluate(
                'exp(f*(V+g*n-mu))*y*sqrt(_n)',
                local_dict=dict(
                    V=self.get_Vext(),
                    g=self.g,
                    n=n,
                    mu=self.mu,
                    _n=self._N/n.sum(),
                    f=self._phase*dt*factor,
                    y=y))
        else:
            V = self.get_Vext() + self.g*n - self.mu
            self.data[...] = np.exp(self._phase*dt*V*factor) * y
            self.data *= np.sqrt(self._N/n.sum())
            

    def mod(self, z):
        """Make sure the point z lies in the box."""
        return complex(*[(_x + _L/2) % (_L) - _L/2
                         for _x, _L in zip((z.real, z.imag), self.Lxy)])

    def step(self, N):
        dt = self.dt
        self.apply_expK(dt=dt, factor=0.5)
        self.t += dt/2.0
        for n in range(N):
            # Update tracer particle positions
            self.update_tracer_velocity()   # Maybe defer if too slow.
            self.update_tracer_pos(dt)
            
            density = self.get_density()
            self.pot_z += dt * self.pot_v
            pot_a = -self.pot_k_m * (self.pot_z - self.z_finger)
            pot_a += -self.pot_damp * self.pot_v
            self.pot_v += dt * pot_a
            v_max = self.get_v_max(density=density)
            if abs(self.pot_v) > v_max:
                self.pot_v *= v_max/abs(self.pot_v)
            self.pot_z = self.mod(self.pot_z)

            self.apply_expV(dt=dt, factor=1.0, density=density)
            self.apply_expK(dt=dt, factor=1.0)
            self.t += dt
            
        self.apply_expK(dt=dt, factor=-0.5)
        self.t -= dt/2.0

        # Update tracer particle velocities after each full loop for speed
        # self.update_tracer_velocity()
        
    def plot(self):
        from matplotlib import pyplot as plt
        n = self.get_density()
        x, y = self.xy
        plt.pcolormesh(x.ravel(), y.ravel(), n.T)
        plt.gca().set_aspect(1)
        plt.plot([self.pot_z.real], [self.pot_z.imag], 'ro')
        plt.plot([self.z_finger.real], [self.z_finger.imag], 'go')
        plt.title("{:.2f}".format(self.t))
        plt.colorbar()
