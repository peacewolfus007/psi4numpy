# A simple Psi 4 script to compute CCSD from a RHF reference
# Scipy and numpy python modules are required
#
# Algorithms were taken directly from Daniel Crawford's programming website:
# http://sirius.chem.vt.edu/wiki/doku.php?id=crawdad:programming
# Special thanks to Lori Burns for integral help
#
# Created by: Daniel G. A. Smith
# Date: 2/22/2015
# License: GPL v3.0
#

import time
import numpy as np

# N dimensional dot
# Like a mini DPD library
def ndot(input_string, op1, op2, prefactor=None):
    """
    No checks, if you get weird errors its up to you to debug.

    ndot('abcd,cdef->abef', arr1, arr2)
    """
    inp, output_ind = input_string.split('->')
    input_left, input_right = inp.split(',')

    size_dict = {}
    for s, size in zip(input_left, op1.shape):
        size_dict[s] = size
    for s, size in zip(input_right, op2.shape):
        size_dict[s] = size

    set_left = set(input_left)
    set_right = set(input_right)
    set_out = set(output_ind)

    idx_removed = (set_left | set_right) - set_out
    keep_left = set_left - idx_removed
    keep_right = set_right - idx_removed

    # Tensordot axes
    left_pos, right_pos = (), ()
    for s in idx_removed:
        left_pos += (input_left.find(s),)
        right_pos += (input_right.find(s),)
    tdot_axes = (left_pos, right_pos)

    # Get result ordering
    tdot_result = input_left + input_right
    for s in idx_removed:
        tdot_result = tdot_result.replace(s, '')
    
    rs = len(idx_removed)
    dim_left, dim_right, dim_removed = 1, 1, 1
    for key, size in size_dict.iteritems():
        if key in keep_left:
            dim_left *= size
        if key in keep_right:
            dim_right *= size
        if key in idx_removed:
            dim_removed *= size

    shape_result = tuple(size_dict[x] for x in tdot_result)
    used_einsum = False

    # Matrix multiply
    # No transpose needed
    if input_left[-rs:] == input_right[:rs]:
        new_view = np.dot(op1.reshape(dim_left, dim_removed),
                          op2.reshape(dim_removed, dim_right))

    # Transpose both
    elif input_left[:rs] == input_right[-rs:]:
        new_view = np.dot(op1.reshape(dim_removed, dim_left).T,
                          op2.reshape(dim_right, dim_removed).T)

    # Transpose right
    elif input_left[-rs:] == input_right[-rs:]:
        new_view = np.dot(op1.reshape(dim_left, dim_removed),
                          op2.reshape(dim_right, dim_removed).T)

    # Tranpose left
    elif input_left[:rs] == input_right[:rs]:
        new_view = np.dot(op1.reshape(dim_removed, dim_left).T,
                          op2.reshape(dim_removed, dim_right))

    # If we have to transpose vector matrix, einsum is faster
    elif (len(keep_left) == 0) or (len(keep_right) == 0):
        new_view = np.einsum(input_string, op1, op2)
        used_einsum = True

    else:
        new_view = np.tensordot(op1, op2, axes=tdot_axes)

    # Make sure the resulting shape is correct
    if (new_view.shape != shape_result) and not used_einsum:
        if (len(shape_result) > 0):
            new_view = new_view.reshape(shape_result)
        else:
            new_view = np.squeeze(new_view)

    # In-place mult by prefactor if requested
    if prefactor is not None:
        new_view *= prefactor

    # Do final tranpose if needed
    if used_einsum:
        return new_view
    elif tdot_result == output_ind:
        return new_view
    else:
        return np.einsum(tdot_result + '->' + output_ind, new_view)


class helper_CCSD(object):

    def __init__(self, psi, energy, mol, memory=2):

        print("\nInitalizing CCSD object...\n")

        # Integral generation from Psi4's MintsHelper
        time_init = time.time()

        print('Computing RHF reference.')
        psi.set_active_molecule(mol)
        psi.set_local_option('SCF', 'SCF_TYPE', 'PK')
        psi.set_local_option('SCF', 'E_CONVERGENCE', 10e-10)
        psi.set_local_option('SCF', 'D_CONVERGENCE', 10e-10)
        self.energy_rhf = energy('RHF')
        print('RHF Final Energy          %.10f' % (self.energy_rhf))

        self.wfn = psi.wavefunction()
        self.C = self.wfn.Ca()
        self.npC = np.asanyarray(self.C)
        self.eps = np.asanyarray(self.wfn.epsilon_a())
        self.ndocc = self.wfn.doccpi()[0]
        self.memory = memory

        mints = psi.MintsHelper()
        H = np.asanyarray(mints.ao_kinetic()) + np.asanyarray(mints.ao_potential())
        self.nmo = H.shape[0]

        # Update H, transform to MO basis and tile for alpha/beta spin
        H = np.einsum('uj,vi,uv', self.npC, self.npC, H)
        H = np.repeat(H, 2, axis=0)
        H = np.repeat(H, 2, axis=1)
        
        # Make H block diagonal
        spin_ind = np.arange(H.shape[0], dtype=np.int) % 2
        H *= (spin_ind.reshape(-1, 1) == spin_ind)
        
        #Make spin-orbital MO
        print('Starting AO -> spin-orbital MO transformation...')

        ERI_Size = (self.nmo ** 4) * 128.e-9
        memory_footprint = ERI_Size * 5
        if memory_footprint > self.memory:
            self.psi.clean()
            raise Exception("Estimated memory utilization (%4.2f GB) exceeds numpy_memory \
                            limit of %4.2f GB." % (memory_footprint, numpy_memory))

        # Integral generation from Psi4's MintsHelper
        self.MO = np.asanyarray(mints.mo_spin_eri(self.C, self.C))
        print("Size of the ERI tensor is %4.2f GB, %d basis functions." % (ERI_Size, self.nmo))
        
        # Update nocc and nvirt
        self.nso = self.nmo * 2
        self.nocc = self.ndocc * 2
        self.nvirt = self.nso - self.nocc
        
        # Make slices
        self.o = slice(0, self.nocc)
        self.v = slice(self.nocc, self.nso)
        self.slice_dict = {'o' : self.o, 'v' : self.v}
        
        #Extend eigenvalues
        self.eps = np.repeat(self.eps, 2)

        # Compute Fock matrix
        self.F = H + np.einsum('pmqm->pq', self.MO[:, self.o, :, self.o])

        ### Build D matrices
        print('\nBuilding denominator arrays.')
        Focc = np.diag(self.F)[self.o]
        Fvir = np.diag(self.F)[self.v]

        self.Dia = Focc.reshape(-1, 1) - Fvir
        self.Dijab = Focc.reshape(-1, 1, 1, 1) + Focc.reshape(-1, 1, 1) - Fvir.reshape(-1, 1) - Fvir

        ### Construct initial guess
        print('Building initial guess.')
        # t^a_i
        self.t1 = np.zeros((self.nocc, self.nvirt))
        # t^{ab}_{ij}
        self.t2 = self.MO[self.o, self.o, self.v, self.v] / self.Dijab
        
        print('..initialed CCSD in %.3f seconds.\n' % (time.time() - time_init))

    # occ orbitals i, j, k, l, m, n
    # virt orbitals a, b, c, d, e, f
    # all oribitals p, q, r, s, t, u, v
    def get_MO(self, string):
        if len(string) != 4:
            self.psi.clean()
            raise Exception('get_MO: string %s must have 4 elements.' % string)
        return self.MO[self.slice_dict[string[0]], self.slice_dict[string[1]],
                       self.slice_dict[string[2]], self.slice_dict[string[3]]]

    def get_F(self, string):
        if len(string) != 2:
            self.psi.clean()
            raise Exception('get_F: string %s must have 4 elements.' % string)
        return self.F[self.slice_dict[string[0]], self.slice_dict[string[1]]]

    
    #Bulid Eqn 9: tilde{\Tau})
    def build_tilde_tau(self):
        ttau = self.t2.copy()
        tmp = 0.5 * np.einsum('ia,jb->ijab', self.t1, self.t1)
        ttau += tmp
        ttau -= tmp.swapaxes(2, 3)
        return ttau
    
    
    #Build Eqn 10: \Tau)
    def build_tau(self):
        ttau = self.t2.copy()
        tmp = np.einsum('ia,jb->ijab', self.t1, self.t1)
        ttau += tmp
        ttau -= tmp.swapaxes(2, 3)
        return ttau
    
    
    #Build Eqn 3:
    def build_Fae(self):
        Fae = self.get_F('vv').copy()
        Fae[np.diag_indices_from(Fae)] = 0
    
        Fae -= ndot('me,ma->ae', self.get_F('ov'), self.t1, prefactor=0.5)
        Fae += ndot('mf,mafe->ae', self.t1, self.get_MO('ovvv'))
    
        Fae -= ndot('mnaf,mnef->ae', self.build_tilde_tau(), self.get_MO('oovv'), prefactor=0.5)
        return Fae
    
    
    #Build Eqn 4:
    def build_Fmi(self):
        Fmi = self.get_F('oo').copy()
        Fmi[np.diag_indices_from(Fmi)] = 0
    
        Fmi += ndot('ie,me->mi', self.t1, self.get_F('ov'), prefactor=0.5)
        Fmi += ndot('ne,mnie->mi', self.t1, self.get_MO('ooov'))
    
        Fmi += ndot('inef,mnef->mi', self.build_tilde_tau(), self.get_MO('oovv'), prefactor=0.5)
        return Fmi
    
    
    #Build Eqn 5:
    def build_Fme(self):
        Fme = self.get_F('ov').copy()
        Fme += ndot('nf,mnef->me', self.t1, self.get_MO('oovv'))
        return Fme
    
    
    #Build Eqn 6:
    def build_Wmnij(self):
        Wmnij = self.get_MO('oooo').copy()        

        Pij = ndot('je,mnie->mnij', self.t1, self.get_MO('ooov'))
        Wmnij += Pij
        Wmnij -= Pij.swapaxes(2, 3)
    
        Wmnij += ndot('ijef,mnef->mnij', self.build_tau(), self.get_MO('oovv'), prefactor=0.25)
        return Wmnij
    
    
    #Build Eqn 7:
    def build_Wabef(self):
        # Rate limiting step written using tensordot, ~10x faster
        # The commented out lines are consistent with the paper
    
        Wabef = self.get_MO('vvvv').copy()
    
        Pab = ndot('mb,amef->abef', self.t1, self.get_MO('vovv'))
        Wabef -= Pab
        Wabef += Pab.swapaxes(0, 1)
    
        Wabef += ndot('mnab,mnef->abef', self.build_tau(), self.get_MO('oovv'), prefactor=0.25)
        return Wabef
    
    
    #Build Eqn 8:
    def build_Wmbej(self):
        Wmbej = self.get_MO('ovvo').copy()
        Wmbej += ndot('jf,mbef->mbej', self.t1, self.get_MO('ovvv'))
        Wmbej -= ndot('nb,mnej->mbej', self.t1, self.get_MO('oovo'))
    
        tmp = (0.5 * self.t2)
        tmp += np.einsum('jf,nb->jnfb', self.t1, self.t1)
 
        Wmbej -= ndot('jnfb,mnef->mbej', tmp, self.get_MO('oovv'))
        return Wmbej
    
    
    def update(self): 
        # Updates amplitudes

        ### Build intermediates
        Fae = self.build_Fae()
        Fmi = self.build_Fmi()
        Fme = self.build_Fme()

        #### Build RHS side of self.t1 equations
        rhs_T1 = self.get_F('ov').copy()
        rhs_T1 += ndot('ie,ae->ia', self.t1, Fae)
        rhs_T1 -= ndot('ma,mi->ia', self.t1, Fmi)
        rhs_T1 += ndot('imae,me->ia', self.t2, Fme)
        rhs_T1 -= ndot('nf,naif->ia', self.t1, self.get_MO('ovov'))
        rhs_T1 -= ndot('imef,maef->ia', self.t2, self.get_MO('ovvv'), prefactor=0.5)
        rhs_T1 -= ndot('mnae,nmei->ia', self.t2, self.get_MO('oovo'), prefactor=0.5)
    
        ### Build RHS side of self.t2 equations
        rhs_T2 = self.get_MO('oovv').copy()
    
        # P_(ab) t_ijae (F_be - 0.5 t_mb F_me)
        tmp = Fae - 0.5 * ndot('mb,me->be', self.t1, Fme)
        Pab = ndot('ijae,be->ijab', self.t2, tmp)
        rhs_T2 += Pab
        rhs_T2 -= Pab.swapaxes(2, 3)
    
        # P_(ij) t_imab (F_mj + 0.5 t_je F_me)
        tmp = Fmi + 0.5 * ndot('je,me->mj', self.t1, Fme)
        Pij = ndot('imab,mj->ijab', self.t2, tmp)
        rhs_T2 -= Pij
        rhs_T2 += Pij.swapaxes(0, 1)
    
        tmp_tau = self.build_tau()
        Wmnij = self.build_Wmnij()
        Wabef = self.build_Wabef()
        rhs_T2 += ndot('mnab,mnij->ijab', tmp_tau, Wmnij, prefactor=0.5)
        rhs_T2 += ndot('ijef,abef->ijab', tmp_tau, Wabef, prefactor=0.5)
    
        # P_(ij) * P_(ab)
        # (ij - ji) * (ab - ba)
        # ijab - ijba -jiab + jiba
        tmp = ndot('ie,mbej->mbij', self.t1, self.get_MO('ovvo'))
        tmp = ndot('ma,mbij->ijab', self.t1, tmp)
        Wmbej = self.build_Wmbej()
        Pijab = ndot('imae,mbej->ijab', self.t2, Wmbej) - tmp

        rhs_T2 += Pijab
        rhs_T2 -= Pijab.swapaxes(2, 3)
        rhs_T2 -= Pijab.swapaxes(0, 1)
        rhs_T2 += Pijab.swapaxes(0, 1).swapaxes(2, 3)
    
        Pij = ndot('ie,abej->ijab', self.t1, self.get_MO('vvvo'))
        rhs_T2 += Pij
        rhs_T2 -= Pij.swapaxes(0, 1)
    
        Pab = ndot('ma,mbij->ijab', self.t1, self.get_MO('ovoo'))
        rhs_T2 -= Pab
        rhs_T2 += Pab.swapaxes(2, 3)
    
        ### Update T1 and T2 amplitudes
        self.t1 = rhs_T1 / self.Dia
        self.t2 = rhs_T2 / self.Dijab
    
    def compute_corr_energy(self): 
        ### Compute CCSD correlation energy using current amplitudes
        CCSDcorr_E = np.einsum('ia,ia->', self.get_F('ov'), self.t1)
        CCSDcorr_E += 0.25 * np.einsum('ijab,ijab->', self.get_MO('oovv'), self.t2)
        CCSDcorr_E += 0.5 * np.einsum('ijab,ia,jb->', self.get_MO('oovv'), self.t1, self.t1)
 
        return CCSDcorr_E 


if __name__ == "__main__":
    arr4 = np.random.rand(4, 4, 4, 4)
    arr2 = np.random.rand(4, 4)

    def test_ndot(string, op1, op2):
        ein_ret = np.einsum(string, op1, op2)
        ndot_ret = ndot(string, op1, op2)
        assert np.allclose(ein_ret, ndot_ret)

    test_ndot('abcd,cdef->abef', arr4, arr4)
    test_ndot('acbd,cdef->abef', arr4, arr4)
    test_ndot('acbd,cdef->abfe', arr4, arr4)
    test_ndot('mnab,mnij->ijab', arr4, arr4)
    test_ndot('cd,cdef->ef', arr2, arr4)
    test_ndot('ce,cdef->df', arr2, arr4)
    test_ndot('nf,naif->ia', arr2, arr4)


