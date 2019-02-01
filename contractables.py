"""
TODO:
    * Check that new einsum contraction works correctly
    * Remove __mul__ and __rmul__ methods when not needed
    * Can I remove all atomic Contractable subclasses?

    * Write docstrings for everything
    * See if I can use structure of bond strings to simplify multiplication
      operations
    
"""
import torch

class Contractable:
    """
    Container for tensors with labeled indices and a global batch size

    The labels for our indices give some high-level knowledge of the tensor
    layout, and permit the contraction of pairs of indices in a more 
    systematic manner. However, much of the actual heavy lifting is done 
    through specific contraction routines in different subclasses

    Attributes:
        tensor (Tensor):    A Pytorch tensor whose first index must be a batch
                            index. Sub-classes of Contractable may put other 
                            restrictions on tensor
        bond_str (str):     A string whose letters each label a separate index 
                            of our tensor, and whose length is required to
                            equal the number of indices
        global_bs (int):    The batch size associated with all Contractables.
                            This is thus shared between all instances as a 
                            class attribute
    """
    # The global batch size
    global_bs = None

    def __init__(self, tensor, bond_str):
        shape = list(tensor.shape)
        num_dim = len(shape)
        str_len = len(bond_str)

        global_bs = Contractable.global_bs
        batch_dim = tensor.size(0)

        # Expand along a new batch dimension if needed
        if ('b' not in bond_str and str_len == num_dim) or \
           ('b' == bond_str[0] and str_len == num_dim + 1):
            if global_bs is not None:
                tensor = tensor.unsqueeze(0).expand([global_bs] + shape)
            else:
                raise RuntimeError("No batch size given and no previous "
                                   "batch size set")
            if bond_str[0] != 'b':
                bond_str = 'b' + bond_str

        # Check for correct formatting in bond_str
        elif bond_str[0] != 'b' or str_len != num_dim:
            raise ValueError("Length of bond string '{bond_str}' "
                            f"({len(bond_str)}) must match order of "
                            f"tensor ({len(shape)})")

        # Set the global batch size if it hasn't been set yet
        elif global_bs is None:
            Contractable.global_bs = batch_dim

        # Check that global batch size agrees with input tensor's first dim
        elif global_bs != batch_dim:
                raise RuntimeError(f"Batch size previously set to {global_bs}"
                                    ", but input tensor has batch size "
                                   f"{batch_dim}. Try calling "
                                    "Contractable.unset_batch_size() first")
        
        # Set the defining attributes of our Contractable
        self.tensor = tensor
        self.bond_str = bond_str

    def __mul__(self, contractable, rmul=False):
        """
        Multiply with another contractable along a linear index

        The default behavior is to multiply the 'r' index of this instance
        with the 'l' index of contractable, matching the batch ('b')
        index of both, and take a Kronecker product of other indices.
        If rmul is True, contractable is instead multiplied on the right.
        """
        # This method works for general contractables besides Scalars, which
        # have a special purpose multiplication (no 'l' and 'r' indices) 
        if isinstance(contractable, Scalar):
            return NotImplemented

        tensors = [self.tensor, contractable.tensor]
        bond_strs = [self.bond_str, contractable.bond_str]
        lowercases = [chr(c) for c in range(ord('a'), ord('z')+1)]

        # Reverse the order of tensors if needed
        if rmul:
            tensors = tensors[::-1]

        # Check that bond strings are in proper format
        for i, bs in enumerate(bond_strs):
            assert bs[0] == 'b'
            assert len(set(bs)) == len(bs)
            assert all([c in lowercases for c in bs])
            if rmul:
                assert (i == 0 and 'r' in bs) or (i == 1 and 'l' in bs)
            else:
                assert (i == 0 and 'r' in bs) or (i == 1 and 'l' in bs)

        # Get used and free characters
        used_chars = set(bond_strs[0]).union(bond_strs[1])
        free_chars = [c for c in lowercases if c not in used_chars]

        # Rename overlapping indices in the bond strings (except 'b', 'l', 'r')
        specials = ['b', 'l', 'r']
        for i, c in enumerate(bond_strs[1]):
            if c in bond_strs[0] and c not in specials:
                bond_strs[1][i] = free_chars.pop()

        # Combine right bond of left tensor and left bond of right tensor
        sum_char = free_chars.pop()
        bond_strs[0][bond_strs[0].index('r')] = sum_char
        bond_strs[1][bond_strs[1].index('l')] = sum_char
        specials.append(sum_char)

        # Build bond string of ouput tensor
        out_str = 'b'
        for bs in bond_strs:
            out_str += ''.join([c for c in bs if c not in specials])
        out_str += 'l' if 'l' in bond_strs[0] else ''
        out_str += 'r' if 'r' in bond_strs[1] else ''

        # Build the einsum string for this operation
        ein_str = f"{bond_strs[0]},{bond_strs[1]}->{out_str}"

        # Contract along the linear dimension and return result
        out_tensor = torch.einsum(ein_str, [tensors[0], tensors[1]])

        return Contractable(out_tensor, out_str)

    def __rmul__(self, contractable):
        """
        Multiply with another contractable along a linear index
        """
        return self.__mul__(contractable, rmul=True)

    def reduce(self):
        """
        Return the contractable without any modification

        reduce() can be any method which returns a contractable. This is
        trivially possible for any contractable by returning itself
        """
        return self

    def set_batch_size(self, batch_size):
        """
        Set the global batch size for all contractables to batch_size
        """
        Contractable.global_bs = batch_size

    def unset_batch_size(self):
        """
        Set the global batch size for all contractables to None
        """
        Contractable.global_bs = None

class MatRegion(Contractable):
    """
    A contiguous collection of matrices which are multiplied together

    The input tensor defining our MatRegion must have shape 
    [batch_size, num_mats, D, D], or [num_mats, D, D] when the global batch
    size is already known
    """
    def __init__(self, mats):
        shape = list(mats.shape)
        if len(shape) not in [3, 4] or shape[-2] != shape[-1]:
            raise ValueError("MatRegion tensors must have shape "
                             "[batch_size, num_mats, D, D], or [num_mats,"
                             " D, D] if batch size has already been set")

        super().__init__(mats, bond_str='bslr')

    def __mul__(self, right_vec):
        """
        Iteratively multiply an input vector on the left with all our matrices
        """
        # The input must be an instance of EdgeVec
        if not isinstance(right_vec, EdgeVec):
            return NotImplemented

        mats = self.tensor
        num_mats = mats.size(1)
        batch_size = mats.size(0)

        # Load our vector and matrix batches
        vec = right_vec.tensor.unsqueeze(2)
        mat_list = [mat.squeeze(1) for mat in torch.chunk(mats, num_mats, 1)]

        # Do the repeated matrix-vector multiplications in right-to-left order
        for mat in mat_list[::-1]:
            vec = torch.bmm(mat, vec)

        # Since we only have a single vector, wrap it as a EdgeVec
        return EdgeVec(vec.squeeze(2), is_left_vec=False)

    def __rmul__(self, left_vec):
        """
        Iteratively multiply an input vector on the right with all our matrices 
        """
        # The input must be an instance of EdgeVec
        if not isinstance(left_vec, EdgeVec):
            return NotImplemented

        mats = self.tensor
        num_mats = mats.size(1)
        batch_size = mats.size(0)

        # Load our vector and matrix batches
        vec = left_vec.tensor.unsqueeze(1)
        mat_list = [mat.squeeze(1) for mat in torch.chunk(mats, num_mats, 1)]

        # Do the repeated matrix-vector multiplications in right-to-left order
        for mat in mat_list:
            vec = torch.bmm(vec, mat)

        # Since we only have a single vector, wrap it as a EdgeVec
        return EdgeVec(vec.squeeze(1), is_left_vec=True)

    def reduce(self):
        """
        Multiplies together all matrices and returns resultant SingleMat

        This method uses iterated batch multiplication to evaluate the full 
        matrix product in depth log(num_mats)
        """
        mats = self.tensor
        shape = list(mats.shape)
        batch_size = mats.size(0)
        size, D = shape[1:3]

        # Iteratively multiply pairs of matrices until there is only one
        while size > 1:
            odd_size = (size % 2 == 1)
            half_size = size // 2
            nice_size = 2 * half_size
        
            even_mats = mats[:, 0:nice_size:2].contiguous()
            odd_mats = mats[:, 1:nice_size:2].contiguous()
            leftover = mats[:, nice_size:]

            # Multiply together all pairs of matrices (except leftovers)
            mats = torch.einsum('bslu,bsur->bslr', [even_mats, odd_mats])
            mats = torch.cat([mats, leftover], 1)

            size = half_size + int(odd_size)

        # Since we only have a single matrix, wrap it as a SingleMat
        return SingleMat(mats.squeeze(1))

class OutputCore(Contractable):
    """
    A single MPS core with a single output index
    """
    def __init__(self, tensor):
        # Check the input shape
        if len(mat.shape) not in [3, 4]:
            raise ValueError("OutputCore tensors must have shape [batch_size, "
                             "output_dim, D_l, D_r], or else [output_dim, D_l,"
                             " D_r] if batch size has already been set")

        super().__init__(mat, bond_str='bolr')

class SingleMat(Contractable):
    """
    A batch of matrices associated with a single location in our MPS
    """
    def __init__(self, mat):
        # Check the input shape
        if len(mat.shape) not in [2, 3]:
            raise ValueError("SingleMat tensors must have shape [batch_size, "
                             "D_l, D_r], or else [D_l, D_r] if batch size "
                             "has already been set")

        super().__init__(mat, bond_str='blr')

    def __mul__(self, right_contractable):
        """
        Multiply an input vector or matrix on the left by our matrix
        """
        # The input must be an instance of EdgeVec or SingleMat
        if not isinstance(right_contractable, (EdgeVec, SingleMat)):
            return NotImplemented
        is_vec = isinstance(right_contractable, EdgeVec)

        left_mat = self.tensor
        right_obj = right_contractable.tensor
        batch_size = left_mat.size(0)

        # Add an extra dimension if we have an input vector
        if is_vec:
            right_obj.unsqueeze_(2)

        # Do the batch multiplication
        out_obj = torch.bmm(left_mat, right_obj)

        # Wrap our output in the appropriate constructor
        if is_vec:
            return EdgeVec(out_obj.squeeze(2), is_left_vec=False)
        else:
            return SingleMat(out_obj)

    def __rmul__(self, left_vec):
        """
        Multiply an input vector on the right with our matrix
        """
        # The input must be an instance of EdgeVec
        if not isinstance(left_vec, EdgeVec):
            return NotImplemented

        mat = self.tensor
        left_vec = left_vec.tensor.unsqueeze(1)
        batch_size = mat.size(0)

        # Do the batch multiplication
        vec = torch.bmm(left_vec, mat)

        # Since we only have a single vector, wrap it as a EdgeVec
        return EdgeVec(vec.squeeze(1), is_left_vec=True)

class EdgeVec(Contractable):
    """
    A batch of vectors associated with an edge of our MPS

    EdgeVec instances are always associated with an edge of an MPS, which 
    requires the is_left_vec flag to be set to True (vector on left edge) or 
    False (vector on right edge)
    """
    def __init__(self, vec, is_left_vec):
        # Check the input shape
        if len(vec.shape) not in [1, 2]:
            raise ValueError("EdgeVec tensors must have shape "
                             "[batch_size, D], or else [D] if batch size "
                             "has already been set")

        # EdgeVecs on left edge will have a right-facing bond, and vice versa
        bond_str = 'b' + ('r' if is_left_vec else 'l')
        super().__init__(vec, bond_str=bond_str)

    def __mul__(self, right_vec):
        """
        Take the inner product of our vector with another vector
        """
        # The input must be an instance of EdgeVec
        if not isinstance(right_vec, EdgeVec):
            return NotImplemented

        left_vec = self.tensor.unsqueeze(1)
        right_vec = right_vec.tensor.unsqueeze(2)
        batch_size = left_vec.size(0)

        # Do the batch inner product
        scalar = torch.bmm(left_vec, right_vec).view([batch_size])

        # Since we only have a single scalar, wrap it as a Scalar
        return Scalar(scalar)

class Scalar(Contractable):
    """
    A batch of scalars
    """
    def __init__(self, scalar):
        # Add dummy dimension if we have a torch scalar
        shape = list(scalar.shape)
        if shape is []:
            scalar = scalar.view([1])
            shape = [1]
            
        # Check the input shape
        if len(shape) != 1:
            raise ValueError("input scalar must be a torch tensor with shape "
                             "[batch_size], or [] or [1] if batch size has "
                             "been set")

        super().__init__(scalar, bond_str='b')

    def __mul__(self, contractable):
        """
        Multiply a contractable by our scalar and return the result
        """
        scalar = self.tensor
        tensor = contractable.tensor
        bond_str = contractable.bond_str

        ein_string = f"{bond_str},b->{bond_str}"
        out_tensor = torch.einsum(ein_string, [tensor, scalar])

        # Wrap the result in the same class right_contractable belongs to
        contract_class = type(contractable)
        if contract_class is not Contractable:
            return contract_class(out_tensor)
        else:
            return Contractable(out_tensor, bond_str)

    def __rmul__(self, contractable):
        # Scalar multiplication is commutative
        return self.__mul__(contractable)