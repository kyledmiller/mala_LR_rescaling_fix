"""DataHandler class that loads and scales data."""
import os

try:
    import horovod.torch as hvd
except ModuleNotFoundError:
    # Warning is thrown by Parameters class
    pass
import numpy as np
import torch
from torch.utils.data import TensorDataset

from mala.common.parallelizer import printout, barrier
from mala.common.parameters import Parameters, DEFAULT_NP_DATA_DTYPE
from mala.datahandling.data_handler_base import DataHandlerBase
from mala.common.parameters import Parameters, ParametersData
from mala.datahandling.data_scaler import DataScaler
from mala.datahandling.snapshot import Snapshot
from mala.datahandling.lazy_load_dataset import LazyLoadDataset
from mala.datahandling.lazy_load_dataset_single import LazyLoadDatasetSingle
from mala.datahandling.fast_tensor_dataset import FastTensorDataset
import time

from pprint import pprint

class DataHandler:
    """
    Loads and scales data. Can only process numpy arrays at the moment.

    Data that is not in a numpy array can be converted using the DataConverter
    class.

    Parameters
    ----------
    parameters : mala.common.parameters.Parameters
    descriptor_calculator : mala.descriptors.descriptor.Descriptor
        Used to do unit conversion on input data. If None, then one will
        be created by this class.

    target_calculator : mala.targets.target.Target
        Used to do unit conversion on output data. If None, then one will
        be created by this class.

    input_data_scaler : mala.datahandling.data_scaler.DataScaler
        Used to scale the input data. If None, then one will be created by
        this class.

    output_data_scaler : mala.datahandling.data_scaler.DataScaler
        Used to scale the output data. If None, then one will be created by
        this class.

    clear_data : bool
        If true (default), the data list will be cleared upon creation of
        the object.
    """

    ##############################
    # Constructors
    ##############################

    def __init__(self, parameters: Parameters, target_calculator=None,
                 descriptor_calculator=None, input_data_scaler=None,
                 output_data_scaler=None, clear_data=True):
        super(DataHandler, self).__init__(parameters,
                                          target_calculator=target_calculator,
                                          descriptor_calculator=
                                          descriptor_calculator)
        # Data will be scaled per user specification.            
        self.input_data_scaler = input_data_scaler
        if self.input_data_scaler is None:
            self.input_data_scaler \
                = DataScaler(self.parameters.input_rescaling_type,
                             use_horovod=self.use_horovod)

        self.output_data_scaler = output_data_scaler
        if self.output_data_scaler is None:
            self.output_data_scaler \
                = DataScaler(self.parameters.output_rescaling_type,
                             use_horovod=self.use_horovod)

        # Dimensionalities of data.
        self.input_dimension = 0
        self.output_dimension = 0
        self.nr_snapshots = 0

        # clustering still needs uniform grids
        if self.parameters.use_clustering:
            self.grid_dimension = [0, 0, 0]
            self.grid_size = 0

        # Actual data points in the different categories.
        self.nr_training_data = 0
        self.nr_test_data = 0
        self.nr_validation_data = 0

        # Number of snapshots in these categories.
        self.nr_training_snapshots = 0
        self.nr_test_snapshots = 0
        self.nr_validation_snapshots = 0

        # Arrays and data sets containing the actual data.
        self.training_data_inputs = torch.empty(0)
        self.validation_data_inputs = torch.empty(0)
        self.test_data_inputs = torch.empty(0)
        self.training_data_outputs = torch.empty(0)
        self.validation_data_outputs = torch.empty(0)
        self.test_data_outputs = torch.empty(0)
        self.training_data_sets = []
        self.validation_data_sets = []
        self.test_data_sets = []

        # Needed for the fast tensor data sets.
        self.mini_batch_size = parameters.running.mini_batch_size
        if clear_data:
            self.clear_data()

    ##############################
    # Properties
    ##############################

    @property
    def input_dimension(self):
        """Feature dimension of input data."""
        return self._input_dimension

    @input_dimension.setter
    def input_dimension(self, new_dimension):
        self._input_dimension = new_dimension

    @property
    def output_dimension(self):
        """Feature dimension of output data."""
        return self._output_dimension

    @output_dimension.setter
    def output_dimension(self, new_dimension):
        self._output_dimension = new_dimension

    ##############################
    # Public methods
    ##############################

    # Adding/Deleting data
    ######################        

    def add_snapshot(self, input_npy_file, input_npy_directory,
                     output_npy_file, output_npy_directory, add_snapshot_as,
                     output_units="1/(eV*A^3)", input_units="None",
                     calculation_output_file="", snapshot_type="numpy",
                     selection_mask=None):
        """
        Add a snapshot to the data pipeline.

        Parameters
        ----------
        input_npy_file : string
            File with saved numpy input array.

        input_npy_directory : string
            Directory containing input_npy_directory.

        output_npy_file : string
            File with saved numpy output array.

        output_npy_directory : string
            Directory containing output_npy_file.

        input_units : string
            Units of input data. See descriptor classes to see which units are
            supported.

        output_units : string
            Units of output data. See target classes to see which units are
            supported.

        calculation_output_file : string
            File with the output of the original snapshot calculation. This is
            only needed when testing multiple snapshots.

        add_snapshot_as : string
            Must be "tr", "va" or "te", the snapshot will be added to the
            snapshot list as training, validation or testing snapshot,
            respectively.

        snapshot_type : string
            Either "numpy" or "openpmd" based on what kind of files you
            want to operate on.

        selection_mask : None or [boolean]
            If None, entire snapshot is loaded, if [boolean], it is used as a
            mask to select which examples are loaded
        """
        if selection_mask is not None and self.parameters.use_lazy_loading:
            raise NotImplementedError("Example selection hasn't been implemented for lazy loading yet.") 
        snapshot = Snapshot(input_npy_file, input_npy_directory,
                            output_npy_file, output_npy_directory,
                            add_snapshot_as,
                            input_units=input_units,
                            output_units=output_units,
                            calculation_output=calculation_output_file,
                            snapshot_type=snapshot_type, selection_mask=selection_mask)
        self.parameters.snapshot_directories_list.append(snapshot)

    def clear_data(self):
        """
        Reset the entire data pipeline.

        Useful when doing multiple investigations in the same python file.
        """
        self.training_data_sets = []
        self.validation_data_sets = []
        self.test_data_sets = []
        self.nr_training_data = 0
        self.nr_test_data = 0
        self.nr_validation_data = 0
        self.nr_training_snapshots = 0
        self.nr_test_snapshots = 0
        self.nr_validation_snapshots = 0
        self.parameters.snapshot_directories_list = []

    # Preparing data
    ######################

    def prepare_data(self, reparametrize_scaler=True, from_arrays_dict=None):
        """
        Prepare the data to be used in a training process.

        This includes:

            - Checking snapshots for consistency
            - Parametrizing the DataScalers (if desired)
            - Building DataSet objects.

        Parameters
        ----------
        reparametrize_scaler : bool
            If True (default), the DataScalers are parametrized based on the
            training data.

        from_arrays_dict : dict or None
            (Allows user to provide data directly from memory)
            Dictionary which assigns an array (values) to each snapshot, e.g.,
            {(0,'inputs') : fp_array, (0, 'outputs') : ldos_array, ...} where 0
            is the index of the snapshot (absolute, not relative to data
            partition) and inputs/outputs indicates the nature of the array.         
            None value indicates the data should be pulled from disk according
            to the snapshot objects.
        """
        # During data loading, there is no need to save target data to
        # calculators.
        # Technically, this would be no issue, but due to technical reasons
        # (i.e. float64 to float32 conversion) saving the data this way
        # may create copies in memory.
        self.target_calculator.save_target_data = False

        # Do a consistency check of the snapshots so that we don't run into
        # an error later. If there is an error, check_snapshots() will raise
        # an exception.
        printout("Checking the snapshots and your inputs for consistency.",
                 min_verbosity=1)
        self.__check_snapshots(from_arrays_dict=from_arrays_dict)
        printout("Consistency check successful.", min_verbosity=0)

        # If the DataHandler is used for inference, i.e. no training or
        # validation snapshots have been provided,
        # than we can definitely not reparametrize the DataScalers.
        if self.nr_training_data == 0:
            reparametrize_scaler = False
            if self.input_data_scaler.cantransform is False or \
                    self.output_data_scaler.cantransform is False:
                raise Exception("In inference mode, the DataHandler needs "
                                "parametrized DataScalers, "
                                "while you provided unparametrized "
                                "DataScalers.")

        # Parametrize the scalers, if needed.
        if reparametrize_scaler:
            printout("Initializing the data scalers.", min_verbosity=1)
            self.__parametrize_scalers(from_arrays_dict=from_arrays_dict)
            printout("Data scalers initialized.", min_verbosity=0)
        elif self.parameters.use_lazy_loading is False and \
                self.nr_training_data != 0:
            printout("Data scalers already initilized, loading data to RAM.",
                     min_verbosity=0)
            self.__load_data("training", "inputs",  from_arrays_dict=from_arrays_dict)
            self.__load_data("training", "outputs", from_arrays_dict=from_arrays_dict)

        # Build Datasets
        printout("Build datasets.", min_verbosity=1)
        self.__build_datasets(from_arrays_dict=from_arrays_dict)
        printout("Build dataset: Done.", min_verbosity=0)

        # After the loading is done, target data can safely be saved again.
        self.target_calculator.save_target_data = True
        
        # Wait until all ranks are finished with data preparation.
        # It is not uncommon that ranks might be asynchronous in their
        # data preparation by a small amount of minutes. If you notice
        # an elongated wait time at this barrier, check that your file system
        # allows for parallel I/O.
        barrier()


    def refresh_data(self, from_arrays_dict=None, partitions=['tr','va','te']):
        """
        Replace tr, va, te data for next generation of active learning.


        Parameters
        ----------

        from_arrays_dict : dict or None
            (Allows user to provide data directly from memory)
            Dictionary which assigns an array (values) to each snapshot, e.g.,
            {(0,'inputs') : fp_array, (0, 'outputs') : ldos_array, ...} where 0
            is the index of the snapshot (absolute, not relative to data
            partition) and inputs/outputs indicates the nature of the array.         
            None value indicates the data should be pulled from disk according
            to the snapshot objects.

        partitions: list
            Specifies the partitions for which to reload data
        """
        # During data loading, there is no need to save target data to
        # calculators.
        # Technically, this would be no issue, but due to technical reasons
        # (i.e. float64 to float32 conversion) saving the data this way
        # may create copies in memory.
        self.target_calculator.save_target_data = False
        
         
        # Reallocate arrays for data storage
        start = time.time()
        if self.parameters.data_splitting_type == "by_snapshot":
            self.nr_training_snapshots, self.nr_training_data, \
            self.nr_test_snapshots, self.nr_test_data, \
            self.nr_validation_snapshots, self.nr_validation_data \
            = 0, 0, 0, 0, 0, 0 
            #pprint(vars(self))
            #pprint(vars(self.parameters))
            snapshot: Snapshot
            # As we are not actually interested in the number of snapshots,
            # but in the number of datasets, we also need to multiply by that.

            for i, snapshot in enumerate(self.parameters.snapshot_directories_list):
                print(f'Snapshot {i}: {snapshot.grid_size}')
                if snapshot.snapshot_function == "tr":
                    self.nr_training_snapshots += 1
                    self.nr_training_data += snapshot.grid_size
                elif snapshot.snapshot_function == "te":
                    self.nr_test_snapshots += 1
                    self.nr_test_data += snapshot.grid_size
                elif snapshot.snapshot_function == "va":
                    self.nr_validation_snapshots += 1
                    self.nr_validation_data += snapshot.grid_size
                else:
                    raise Exception("Unknown option for snapshot splitting "
                                    "selected.")

            # Now we need to check whether or not this input is believable.
            nr_of_snapshots = len(self.parameters.snapshot_directories_list)
            if nr_of_snapshots != (self.nr_training_snapshots +
                                   self.nr_test_snapshots +
                                   self.nr_validation_snapshots):
                raise Exception("Cannot split snapshots with specified "
                                "splitting scheme, "
                                "too few or too many options selected: "
                                f"[{nr_of_snapshots} != {self.nr_training_snapshots} + {self.nr_test_snapshots} + {self.nr_validation_snapshots}]")

            # MALA can either be run in training or test-only mode.
            # But it has to be run in either of those!
            # So either training AND validation snapshots can be provided
            # OR only test snapshots.
            if self.nr_test_snapshots != 0:
                if self.nr_training_snapshots == 0:
                    printout("DataHandler prepared for inference. No training "
                             "possible with this setup. If this is not what "
                             "you wanted, please revise the input script. "
                             "Validation snapshots you may have entered will"
                             "be ignored.",
                             min_verbosity=0)
            else:
                if self.nr_training_snapshots == 0:
                    raise Exception("No training snapshots provided.")
                if self.nr_validation_snapshots == 0:
                    raise Exception("No validation snapshots provided.")
        else:
            raise Exception("Wrong parameter for data splitting provided.")

        self.__allocate_arrays()
        printout(f'ttt allocate:                         {time.time()-start}s')
    
        ### Load updated data
        start = time.time()
        expand_partition_name = {'tr':'training', 
                                 'va':'validation', 
                                 'te':'test'}
        for partition in partitions:
            self.__load_data(expand_partition_name[partition], "inputs", 
                             from_arrays_dict=from_arrays_dict)
            self.__load_data(expand_partition_name[partition], "outputs", 
                             from_arrays_dict=from_arrays_dict)
        printout(f'ttt refresh:                          {time.time()-start}s')

        # After the loading is done, target data can safely be saved again.
        self.target_calculator.save_target_data = True

        printout("Build datasets.", min_verbosity=1)
        self.__build_datasets(from_arrays_dict=from_arrays_dict)
        printout("Build dataset: Done.", min_verbosity=0)
        printout(f'ttt build_dataset:                    {time.time()-start}s')

        # Wait until all ranks are finished with data preparation.
        # It is not uncommon that ranks might be asynchronous in their
        # data preparation by a small amount of minutes. If you notice
        # an elongated wait time at this barrier, check that your file system
        # allows for parallel I/O.
        barrier()

    def prepare_for_testing(self):
        """
        Prepare DataHandler for usage within Tester class.

        Ensures that lazily-loaded data sets do not perform unnecessary I/O
        operations. Only needed in Tester class.
        """
        if self.parameters.use_lazy_loading:
            self.test_data_set.return_outputs_directly = True

    # Training  / Testing
    ######################

    def mix_datasets(self):
        """
        For lazily-loaded data sets, the snapshot ordering is (re-)mixed.

        This applies only to the training data set. For the validation and
        test set it does not matter.
        """
        if self.parameters.use_lazy_loading:
            for dset in self.training_data_sets:
                dset.mix_datasets()

    def get_test_input_gradient(self, snapshot_number):
        """
        Get the gradient of the test inputs for an entire snapshot.

        This gradient will be returned as scaled Tensor.
        The reason the gradient is returned (rather then returning the entire
        inputs themselves) is that by slicing a variable, pytorch no longer
        considers it a "leaf" variable and will stop tracking and evaluating
        its gradient. Thus, it is easier to obtain the gradient and then
        slice it.

        Parameters
        ----------
        snapshot_number : int
            Number of the snapshot for which the entire test inputs.

        Returns
        -------
        torch.Tensor
            Tensor holding the gradient.

        """
        # get the snapshot from the snapshot number
        snapshot = self.parameters.snapshot_directories_list[snapshot_number]
        
        if self.parameters.use_lazy_loading:
            # This fails if an incorrect snapshot was loaded.
            if self.test_data_sets[0].currently_loaded_file != snapshot_number:
                raise Exception("Cannot calculate gradients, wrong file "
                                "was lazily loaded.")
            return self.test_data_sets[0].input_data.grad
        else:
            return self.test_data_inputs.\
                       grad[snapshot.grid_size*snapshot_number:
                            snapshot.grid_size*(snapshot_number+1)]

    def get_snapshot_calculation_output(self, snapshot_number):
        """
        Get the path to the output file for a specific snapshot.

        Parameters
        ----------
        snapshot_number : int
            Snapshot for which the calculation output should be returned.

        Returns
        -------
        calculation_output : string
            Path to the calculation output for this snapshot.

        """
        return self.parameters.snapshot_directories_list[snapshot_number].\
            calculation_output

    # Debugging
    ######################
        
    def raw_numpy_to_converted_scaled_tensor(self, numpy_array, data_type,
                                             units, convert3Dto1D=False):
        """
        Transform a raw numpy array into a scaled torch tensor.

        This tensor will also be in the right units, i.e. a tensor that can
        simply be put into a MALA network.

        Parameters
        ----------
        numpy_array : np.array
            Array that is to be converted.
        data_type : string
            Either "in" or "out", depending if input or output data is
            processed.
        units : string
            Units of the data that is processed.
        convert3Dto1D : bool
            If True (default: False), then a (x,y,z,dim) array is transformed
            into a (x*y*z,dim) array.

        Returns
        -------
        converted_tensor: torch.Tensor
            The fully converted and scaled tensor.
        """
        # Check parameters for consistency.
        if data_type != "in" and data_type != "out":
            raise Exception("Please specify either \"in\" or \"out\" as "
                            "data_type.")

        # Convert units of numpy array.
        numpy_array = self.__raw_numpy_to_converted_numpy(numpy_array,
                                                          data_type, units)

        # If desired, the dimensions can be changed.
        if convert3Dto1D:
            if data_type == "in":
                data_dimension = self.input_dimension
            else:
                data_dimension = self.output_dimension
            grid_size = np.prod(numpy_array[0:3])
            desired_dimensions = [grid_size, data_dimension]
        else:
            desired_dimensions = None

        # Convert numpy array to scaled tensor a network can work with.
        numpy_array = self.\
            __converted_numpy_to_scaled_tensor(numpy_array, desired_dimensions,
                                               data_type)
        return numpy_array

    def resize_snapshots_for_debugging(self, directory="./",
                                       naming_scheme_input=
                                       "test_Al_debug_2k_nr*.in",
                                       naming_scheme_output=
                                       "test_Al_debug_2k_nr*.out"):
        """
        Resize all snapshots in the list.

        Parameters
        ----------
        directory : string
            Directory to which the resized snapshots should be saved.

        naming_scheme_input : string
            Naming scheme for the resulting input numpy files.

        naming_scheme_output : string
            Naming scheme for the resulting output numpy files.

        """
        i = 0
        snapshot: Snapshot
        for snapshot in self.parameters.snapshot_directories_list:
            tmp_array = self.descriptor_calculator.\
                read_from_numpy_file(os.path.join(snapshot.input_npy_directory,
                                                  snapshot.input_npy_file),
                                     units=snapshot.input_units)
            tmp_file_name = naming_scheme_input
            tmp_file_name = tmp_file_name.replace("*", str(i))
            np.save(os.path.join(directory, tmp_file_name) + ".npy", tmp_array)

            tmp_array = self.target_calculator.\
                read_from_numpy_file(os.path.join(snapshot.output_npy_directory,
                                                  snapshot.output_npy_file),
                                     units=snapshot.output_units)
            tmp_file_name = naming_scheme_output
            tmp_file_name = tmp_file_name.replace("*", str(i))
            np.save(os.path.join(directory, tmp_file_name + ".npy"), tmp_array)
            i += 1

    ##############################
    # Private methods
    ##############################

    # Loading data
    ######################

    def __check_snapshots(self, from_arrays_dict=None):
        """Check the snapshots for consistency."""
        self.nr_snapshots = len(self.parameters.snapshot_directories_list)

        # Read the snapshots using a memorymap to see if there is consistency.
        firstsnapshot = True
        for i, snapshot in enumerate(self.parameters.snapshot_directories_list):
            ####################
            # Descriptors.
            ####################            

            printout("Checking descriptor file ", snapshot.input_npy_file,
                     "at", snapshot.input_npy_directory, min_verbosity=1)
            if from_arrays_dict is not None:
                print(f'arrdim:   {from_arrays_dict[(i, "inputs")].shape}')
                print(f'featmask: {self.descriptor_calculator._feature_mask()}')
                tmp_dimension = from_arrays_dict[(i, 'inputs')]\
                    [:,self.descriptor_calculator._feature_mask():].shape
                # We don't need any reference to full grid dim at this point
                # so this is just for compatibility w other code
                if len(tmp_dimension) > 2: 
                    raise ValueError('Flatten the data pool arrays.')
                tmp_dimension = (tmp_dimension[0], 1, 1, tmp_dimension[-1])
                printout(f"from_arrays_dict dim {i}: {from_arrays_dict[(i, 'inputs')].shape}")
            elif snapshot.snapshot_type == "numpy":
                tmp_dimension = self.descriptor_calculator.\
                    read_dimensions_from_numpy_file(
                    os.path.join(snapshot.input_npy_directory,
                                 snapshot.input_npy_file))#, 
                    #selection_mask=snapshot._selection_mask)
            elif snapshot.snapshot_type == "openpmd":
                tmp_dimension = self.descriptor_calculator.\
                    read_dimensions_from_openpmd_file(
                    os.path.join(snapshot.input_npy_directory,
                                 snapshot.input_npy_file))
            else:
                raise Exception("Unknown snapshot file type.")
            
            # get the snapshot feature dimension - call it input dimension
            # for flexible grid sizes only this need be consistent
            tmp_input_dimension = tmp_dimension[-1]
            tmp_grid_dim = tmp_dimension[0:3]
            
            # If using selection_mask, apply to dimensions 
            if snapshot._selection_mask is not None:
                tmp_grid_dim = (sum(snapshot._selection_mask),1,1)
            
            print(f'tmp_input_dim {i}: {tmp_input_dimension}')
            print(f'tmp_grid_dim {i}:  {tmp_grid_dim}')
            snapshot.grid_dimension = tmp_grid_dim
            snapshot.grid_size = int(np.prod(tmp_grid_dim))            
            printout(f'grid_size: {snapshot.grid_size}')
            if firstsnapshot:
                self.input_dimension = tmp_input_dimension
                if self.parameters.use_clustering:
                    self.grid_dimension[0:3] = tmp_grid_dim[0:3]
                    self.grid_size = np.prod(self.grid_dimension)
            else:
                if self.input_dimension != tmp_input_dimension:
                    raise Exception("Invalid snapshot entered at ", snapshot.
                                    input_npy_file)
            ####################
            # Targets.
            ####################

            printout("Checking targets file ", snapshot.output_npy_file, "at",
                     snapshot.output_npy_directory, min_verbosity=1)
            if from_arrays_dict is not None:
                tmp_dimension = from_arrays_dict[(i, 'outputs')]\
                    [:,self.target_calculator._feature_mask():].shape
                # We don't need any reference to full grid dim at this point
                # so this is just for compatibility w other code
                if len(tmp_dimension) > 2: 
                    raise ValueError('Flatten the data pool arrays.')
                tmp_dimension = (tmp_dimension[0], 1, 1, tmp_dimension[-1])
            elif snapshot.snapshot_type == "numpy":
                tmp_dimension = self.target_calculator.\
                    read_dimensions_from_numpy_file(
                    os.path.join(snapshot.output_npy_directory,
                                 snapshot.output_npy_file))#,
                    #selection_mask=snapshot._selection_mask)
            elif snapshot.snapshot_type == "openpmd":
                tmp_dimension = self.target_calculator.\
                    read_dimensions_from_openpmd_file(
                    os.path.join(snapshot.output_npy_directory,
                                 snapshot.output_npy_file))
            else:
                raise Exception("Unknown snapshot file type.")

            # The first snapshot determines the data size to be used.
            # We need to make sure that snapshot size is consistent.
            tmp_output_dimension = tmp_dimension[-1]
            if firstsnapshot:
                self.output_dimension = tmp_output_dimension
            else:
                if self.output_dimension != tmp_output_dimension:
                    raise Exception("Invalid snapshot entered at ", snapshot.
                                    output_npy_file)
                
            if firstsnapshot:
                firstsnapshot = False                

        # Now we need to confirm that the snapshot list has some inner
        # consistency.
        if self.parameters.data_splitting_type == "by_snapshot":
            #pprint(vars(self))
            #pprint(vars(self.parameters))
            snapshot: Snapshot
            # As we are not actually interested in the number of snapshots,
            # but in the number of datasets, we also need to multiply by that.
            for snapshot in self.parameters.snapshot_directories_list:
                print(snapshot.snapshot_function)
                if snapshot.snapshot_function == "tr":
                    self.nr_training_snapshots += 1
                    self.nr_training_data += snapshot.grid_size
                elif snapshot.snapshot_function == "te":
                    self.nr_test_snapshots += 1
                    self.nr_test_data += snapshot.grid_size
                elif snapshot.snapshot_function == "va":
                    self.nr_validation_snapshots += 1
                    self.nr_validation_data += snapshot.grid_size
                else:
                    raise Exception("Unknown option for snapshot splitting "
                                    "selected.")

            # Now we need to check whether or not this input is believable.
            nr_of_snapshots = len(self.parameters.snapshot_directories_list)
            if nr_of_snapshots != (self.nr_training_snapshots +
                                   self.nr_test_snapshots +
                                   self.nr_validation_snapshots):
                raise Exception("Cannot split snapshots with specified "
                                "splitting scheme, "
                                "too few or too many options selected: "
                                f"[{nr_of_snapshots} != {self.nr_training_snapshots} + {self.nr_test_snapshots} + {self.nr_validation_snapshots}]")
            # MALA can either be run in training or test-only mode.
            # But it has to be run in either of those!
            # So either training AND validation snapshots can be provided
            # OR only test snapshots.
            if self.nr_test_snapshots != 0:
                if self.nr_training_snapshots == 0:
                    printout("DataHandler prepared for inference. No training "
                             "possible with this setup. If this is not what "
                             "you wanted, please revise the input script. "
                             "Validation snapshots you may have entered will"
                             "be ignored.",
                             min_verbosity=0)
            else:
                if self.nr_training_snapshots == 0:
                    raise Exception("No training snapshots provided.")
                if self.nr_validation_snapshots == 0:
                    raise Exception("No validation snapshots provided.")
        else:
            raise Exception("Wrong parameter for data splitting provided.")

        if not self.parameters.use_lazy_loading:
            self.__allocate_arrays()        

        # Reordering the lists.
        snapshot_order = {'tr': 0, 'va': 1, 'te': 2}
        self.parameters.snapshot_directories_list.sort(key=lambda d:
                                                       snapshot_order
                                                       [d.snapshot_function])

    def __allocate_arrays(self):
        if self.nr_training_data > 0:
            self.training_data_inputs = np.zeros((self.nr_training_data,
                                                  self.input_dimension),
                                                 dtype=DEFAULT_NP_DATA_DTYPE)
            self.training_data_outputs = np.zeros((self.nr_training_data,
                                                   self.output_dimension),
                                                  dtype=DEFAULT_NP_DATA_DTYPE)

        if self.nr_validation_data > 0:
            self.validation_data_inputs = np.zeros((self.nr_validation_data,
                                                    self.input_dimension),
                                                   dtype=DEFAULT_NP_DATA_DTYPE)
            self.validation_data_outputs = np.zeros((self.nr_validation_data,
                                                     self.output_dimension),
                                                    dtype=DEFAULT_NP_DATA_DTYPE)

        if self.nr_test_data > 0:
            self.test_data_inputs = np.zeros((self.nr_test_data,
                                              self.input_dimension),
                                             dtype=DEFAULT_NP_DATA_DTYPE)
            self.test_data_outputs = np.zeros((self.nr_test_data,
                                               self.output_dimension),
                                              dtype=DEFAULT_NP_DATA_DTYPE)

    def __load_data(self, function, data_type, from_arrays_dict=None):
        """
        Load data into the appropriate arrays.

        Also transforms them into torch tensors.

        Parameters
        ----------
        function : string
            Can be "tr", "va" or "te.
        data_type : string
            Can be "input" or "output".
        """
        start = time.time()

        if function != "training" and function != "test" and \
                function != "validation":
            raise Exception("Unknown snapshot type detected.")
        if data_type != "outputs" and data_type != "inputs":
            raise Exception("Unknown data type detected.")
        # Extracting all the information pertaining to the data set.
        array = function+"_data_"+data_type
        if data_type == "inputs":
            calculator = self.descriptor_calculator
        else:
            calculator = self.target_calculator

        feature_dimension = self.input_dimension if data_type == "inputs" \
            else self.output_dimension

        snapshot_counter = 0
        gs_old = 0

        print(f'ttt load_data_{function} 0 initialize:   {time.time() - start}')

        for i, snapshot in enumerate(self.parameters.snapshot_directories_list):
            mid = time.time()

            # get the snapshot grid size
            gs_new = snapshot.grid_size

            # Data scaling is only performed on the training data sets.
            if snapshot.snapshot_function == function[0:2]:
                if data_type == "inputs":
                    file = os.path.join(snapshot.input_npy_directory,
                                        snapshot.input_npy_file)
                    units = snapshot.input_units
                else:
                    file = os.path.join(snapshot.output_npy_directory,
                                        snapshot.output_npy_file)
                    units = snapshot.output_units

                # Pull from existing array rather than file
                if from_arrays_dict is not None:
                    if snapshot._selection_mask is not None: gs_new = sum(snapshot._selection_mask)
                    print(f'gs_new {i}: {gs_new}')
                    print(f'Fastloaded {i}, {data_type}: {from_arrays_dict[(i, data_type)].shape}')# -  {from_arrays_dict[(i, data_type)]}')
                    print(f'selmask -> {gs_new}')
                    print(f'indices: {gs_old}:{gs_old+gs_new} in {getattr(self,array).shape}')
                    #print(f'units = {units}')
                    #arr0 = from_arrays_dict[(i, data_type)]
                    #print(f'arr0 {arr0.shape}')#:  {arr0}')
                    #arr1 = from_arrays_dict[(i, data_type)][:, calculator._feature_mask():]
                    #print(f'arr1 {arr1.shape}')#:  {arr1}')
                    #if snapshot._selection_mask is not None:
                    #    arr2 = from_arrays_dict[(i, data_type)][:, calculator._feature_mask():][snapshot._selection_mask]
                    #    print(f'arr2 {arr2.shape}')#:  {arr1}')
                    #    del arr2
                    #del arr0, arr1
                    #calculator._process_loaded_array(from_arrays_dict[i][:, :, :, calculator._feature_mask():][snapshot._selection_mask], units=units)
                    #print(f'arr2 {arr1.shape}:  {arr1}')

                    #TODO streamline this
                    if snapshot._selection_mask is not None:
                        # Update data already in tensor form
                        if torch.is_tensor(getattr(self, array)): 
                            getattr(self, array)[gs_old : gs_old + gs_new, :] =\
                                torch.from_numpy(from_arrays_dict[(i, data_type)]\
                                [:, calculator._feature_mask():]\
                                [snapshot._selection_mask])

                        # Update a fresh numpy array
                        else:        
                            getattr(self, array)[gs_old : gs_old + gs_new, :] =\
                                from_arrays_dict[(i, data_type)]\
                                [:, calculator._feature_mask():]\
                                [snapshot._selection_mask]
                    else:
                        # Update data already in tensor form
                        if torch.is_tensor(getattr(self, array)): 
                            getattr(self, array)[gs_old : gs_old + gs_new, :] =\
                                torch.from_numpy(from_arrays_dict[(i, data_type)]\
                                [:, calculator._feature_mask():])
                        # Update a fresh numpy array
                        else:        
                            getattr(self, array)[gs_old : gs_old + gs_new, :] =\
                                from_arrays_dict[(i, data_type)]\
                                [:, calculator._feature_mask():]


                    print(f'ttt load_data 1 existing_assign:                   {time.time() - mid}')
                    mid = time.time()

                    calculator._process_loaded_array(getattr(self,array)[gs_old : gs_old + gs_new, :], units=units)
                    
                    print(f'ttt load_data 2 existing_process:                  {time.time() - mid}')
                    mid = time.time()

                    #print(f'Fastloaded: {getattr(self,array)[gs_old : gs_old + gs_new, :].shape} {getattr(self,array)[gs_old : gs_old + gs_new, :]}')

                # Pull directly from file
                elif snapshot.snapshot_type == "numpy":
                    calculator.read_from_numpy_file(
                        file,
                        units=units,
                        array=getattr(self, array)[gs_old : gs_old + gs_new, :],
                        reshape=True, selection_mask=snapshot._selection_mask
                    )
                elif snapshot.snapshot_type == "openpmd":
                    if snapshot._selection_mask is not None: raise NotImplementedError('Selection mask is not implemented for openpmd')
                    getattr(self, array)[gs_old : gs_old + gs_new] = \
                        calculator.read_from_openpmd_file(file, units=units) \
                        .reshape([gs_new, feature_dimensions])
                else:
                    raise Exception("Unknown snapshot file type.")
                snapshot_counter += 1
                gs_old += gs_new

        # The scalers will later operate on torch Tensors so we have to
        # make sure they are fitted on
        # torch Tensors as well. Preprocessing the numpy data as follows
        # does NOT load it into memory, see
        # test/tensor_memory.py
        # Also, the following bit does not work with getattr, so I had to
        # hard code it. If someone has a smart idea to circumvent this, I am
        # all ears
        if not torch.is_tensor(getattr(self, array)): 
            if data_type == "inputs":
                if function == "training":
                    self.training_data_inputs = torch.\
                        from_numpy(self.training_data_inputs).float()

                if function == "validation":
                    self.validation_data_inputs = torch.\
                        from_numpy(self.validation_data_inputs).float()

                if function == "test":
                    self.test_data_inputs = torch.\
                        from_numpy(self.test_data_inputs).float()

            if data_type == "outputs":
                if function == "training":
                    self.training_data_outputs = torch.\
                        from_numpy(self.training_data_outputs).float()

                if function == "validation":
                    self.validation_data_outputs = torch.\
                        from_numpy(self.validation_data_outputs).float()

                if function == "test":
                    self.test_data_outputs = torch.\
                        from_numpy(self.test_data_outputs).float()

            print(f'ttt load_data 3 existing_tensorize:      {time.time() - mid}')
            print(f'tttt load_data 4 total:                  {time.time() - start}')
                
    def __build_datasets(self, from_arrays_dict=None):
        """Build the DataSets that are used during training."""
        if self.parameters.use_lazy_loading and not self.parameters.use_lazy_loading_prefetch:

            # Create the lazy loading data sets.
            self.training_data_sets.append(LazyLoadDataset(
                self.input_dimension, self.output_dimension,
                self.input_data_scaler, self.output_data_scaler,
                self.descriptor_calculator, self.target_calculator,
                self.use_horovod))
            self.validation_data_sets.append(LazyLoadDataset(
                self.input_dimension, self.output_dimension,
                self.input_data_scaler, self.output_data_scaler,
                self.descriptor_calculator, self.target_calculator,
                self.use_horovod))

            if self.nr_test_data != 0:
                self.test_data_sets.append(LazyLoadDataset(
                    self.input_dimension,
                    self.output_dimension,
                    self.input_data_scaler, self.output_data_scaler,
                    self.descriptor_calculator, self.target_calculator,
                    self.use_horovod,
                    input_requires_grad=True))

            # Add snapshots to the lazy loading data sets.
            for snapshot in self.parameters.snapshot_directories_list:
                if snapshot.snapshot_function == "tr":
                    self.training_data_sets[0].add_snapshot_to_dataset(snapshot)
                if snapshot.snapshot_function == "va":
                    self.validation_data_sets[0].add_snapshot_to_dataset(snapshot)
                if snapshot.snapshot_function == "te":
                    self.test_data_sets[0].add_snapshot_to_dataset(snapshot)

            # I don't think we need to mix them here. We can use the standard
            # ordering for the first epoch
            # and mix it up after.
            # self.training_data_set.mix_datasets()
            # self.validation_data_set.mix_datasets()
            # self.test_data_set.mix_datasets()
        elif self.parameters.use_lazy_loading and self.parameters.use_lazy_loading_prefetch:
            printout("Using lazy loading pre-fetching.", min_verbosity=2)
            # Create LazyLoadDatasetSingle instances per snapshot and add to
            # list.
            for snapshot in self.parameters.snapshot_directories_list:
                if snapshot.snapshot_function == "tr":
                    self.training_data_sets.append(LazyLoadDatasetSingle(
                        self.mini_batch_size, snapshot,
                        self.input_dimension, self.output_dimension,
                        self.input_data_scaler, self.output_data_scaler,
                        self.descriptor_calculator, self.target_calculator,
                        self.use_horovod))
                if snapshot.snapshot_function == "va":
                    self.validation_data_sets.append(LazyLoadDatasetSingle(
                        self.mini_batch_size, snapshot,
                        self.input_dimension, self.output_dimension,
                        self.input_data_scaler, self.output_data_scaler,
                        self.descriptor_calculator, self.target_calculator,
                        self.use_horovod))
                if snapshot.snapshot_function == "te":
                    self.test_data_sets.append(LazyLoadDatasetSingle(
                        self.mini_batch_size, snapshot,
                        self.input_dimension, self.output_dimension,
                        self.input_data_scaler, self.output_data_scaler,
                        self.descriptor_calculator, self.target_calculator,
                        self.use_horovod,
                        input_requires_grad=True))

        else:
            if self.nr_training_data != 0:
                self.input_data_scaler.transform(self.training_data_inputs)
                self.output_data_scaler.transform(self.training_data_outputs)
                if self.parameters.use_fast_tensor_data_set:
                    printout("Using FastTensorDataset.", min_verbosity=2)
                    self.training_data_sets.append( \
                        FastTensorDataset(self.mini_batch_size,
                                          self.training_data_inputs,
                                          self.training_data_outputs))
                else:
                    self.training_data_sets.append( \
                        TensorDataset(self.training_data_inputs,
                                      self.training_data_outputs))

            if self.nr_validation_data != 0:
                self.__load_data("validation", "inputs", from_arrays_dict=from_arrays_dict)
                self.input_data_scaler.transform(self.validation_data_inputs)

                self.__load_data("validation", "outputs", from_arrays_dict=from_arrays_dict)
                self.output_data_scaler.transform(self.validation_data_outputs)
                if self.parameters.use_fast_tensor_data_set:
                    printout("Using FastTensorDataset.", min_verbosity=2)
                    self.validation_data_sets.append( \
                        FastTensorDataset(self.mini_batch_size,
                                          self.validation_data_inputs,
                                          self.validation_data_outputs))
                else:
                    self.validation_data_sets.append( \
                        TensorDataset(self.validation_data_inputs,
                                      self.validation_data_outputs))

            if self.nr_test_data != 0:
                self.__load_data("test", "inputs", from_arrays_dict=from_arrays_dict)
                self.input_data_scaler.transform(self.test_data_inputs)
                self.test_data_inputs.requires_grad = True

                self.__load_data("test", "outputs", from_arrays_dict=from_arrays_dict)
                self.output_data_scaler.transform(self.test_data_outputs)
                self.test_data_sets.append( \
                    TensorDataset(self.test_data_inputs,
                                  self.test_data_outputs))
        printout(f'# training data:   {self.nr_training_data}',   min_verbosity=2)
        printout(f'# validation data: {self.nr_validation_data}', min_verbosity=2)
        printout(f'# testing data:    {self.nr_test_data}',       min_verbosity=2)

    # Scaling
    ######################

    def __parametrize_scalers(self, from_arrays_dict=None):
        """Use the training data to parametrize the DataScalers."""
        ##################
        # Inputs.
        ##################

        # If we do lazy loading, we have to iterate over the files one at a
        # time and add them to the fit, i.e. incrementally updating max/min
        # or mean/std. If we DON'T do lazy loading, we can simply load the
        # training data (we will need it later anyway) and perform the
        # scaling. This should save some performance.

        if self.parameters.use_lazy_loading:
            self.input_data_scaler.start_incremental_fitting()
            # We need to perform the data scaling over the entirety of the
            # training data.
            for snapshot in self.parameters.snapshot_directories_list:
                # Data scaling is only performed on the training data sets.
                if snapshot.snapshot_function == "tr":
                    if snapshot.snapshot_type == "numpy":
                        tmp = self.descriptor_calculator. \
                            read_from_numpy_file(os.path.join(snapshot.input_npy_directory,
                                                              snapshot.input_npy_file),
                                                 units=snapshot.input_units, 
                                                 selection_mask=snapshot._selection_mask)
                    elif snapshot.snapshot_type == "openpmd":
                        tmp = self.descriptor_calculator. \
                            read_from_openpmd_file(os.path.join(snapshot.input_npy_directory,
                                                                snapshot.input_npy_file))
                    else:
                        raise Exception("Unknown snapshot file type.")

                    # The scalers will later operate on torch Tensors so we
                    # have to make sure they are fitted on
                    # torch Tensors as well. Preprocessing the numpy data as
                    # follows does NOT load it into memory, see
                    # test/tensor_memory.py
                    tmp = np.array(tmp)
                    if tmp.dtype != DEFAULT_NP_DATA_DTYPE:
                        tmp = tmp.astype(DEFAULT_NP_DATA_DTYPE)
                    tmp = tmp.reshape([snapshot.grid_size,
                                       self.input_dimension])
                    tmp = torch.from_numpy(tmp).float()
                    self.input_data_scaler.incremental_fit(tmp)

            self.input_data_scaler.finish_incremental_fitting()

        else:
            self.__load_data("training", "inputs", from_arrays_dict=from_arrays_dict)
            self.input_data_scaler.fit(self.training_data_inputs)

        printout("Input scaler parametrized.", min_verbosity=1)

        ##################
        # Output.
        ##################

        # If we do lazy loading, we have to iterate over the files one at a
        # time and add them to the fit,
        # i.e. incrementally updating max/min or mean/std.
        # If we DON'T do lazy loading, we can simply load the training data
        # (we will need it later anyway)
        # and perform the scaling. This should save some performance.

        if self.parameters.use_lazy_loading:
            i = 0
            self.output_data_scaler.start_incremental_fitting()
            # We need to perform the data scaling over the entirety of the
            # training data.
            for snapshot in self.parameters.snapshot_directories_list:
                if snapshot._selection_mask is not None:  
                    raise NotImplementedError("Example selection hasn't been implemented for lazy loading yet.") 
                # Data scaling is only performed on the training data sets.
                if snapshot.snapshot_function == "tr":
                    if snapshot.snapshot_type == "numpy":
                        tmp = self.target_calculator.\
                            read_from_numpy_file(os.path.join(snapshot.output_npy_directory,
                                                              snapshot.output_npy_file),
                                                 units=snapshot.output_units)
                    elif snapshot.snapshot_type == "openpmd":
                        tmp = self.target_calculator. \
                            read_from_openpmd_file(os.path.join(snapshot.output_npy_directory,
                                                                snapshot.output_npy_file))
                    else:
                        raise Exception("Unknown snapshot file type.")

                    # The scalers will later operate on torch Tensors so we
                    # have to make sure they are fitted on
                    # torch Tensors as well. Preprocessing the numpy data as
                    # follows does NOT load it into memory, see
                    # test/tensor_memory.py
                    tmp = np.array(tmp)
                    if tmp.dtype != DEFAULT_NP_DATA_DTYPE:
                        tmp = tmp.astype(DEFAULT_NP_DATA_DTYPE)
                    tmp = tmp.reshape([snapshot.grid_size,
                                       self.output_dimension])
                    tmp = torch.from_numpy(tmp).float()
                    self.output_data_scaler.incremental_fit(tmp)
                i += 1
            self.output_data_scaler.finish_incremental_fitting()

        else:
            self.__load_data("training", "outputs", from_arrays_dict=from_arrays_dict)
            self.output_data_scaler.fit(self.training_data_outputs)

        printout("Output scaler parametrized.", min_verbosity=1)                

    def __raw_numpy_to_converted_numpy(self, numpy_array, data_type="in",
                                       units=None):
        """Convert a raw numpy array containing into the correct units."""
        if data_type == "in":
            if data_type == "in" and self.descriptor_calculator.\
                    descriptors_contain_xyz:
                numpy_array = numpy_array[:, :, :, 3:]
            if units is not None:
                numpy_array *= self.descriptor_calculator.convert_units(1,
                                                                        units)
            return numpy_array
        elif data_type == "out":
            if units is not None:
                numpy_array *= self.target_calculator.convert_units(1, units)
            return numpy_array
        else:
            raise Exception("Please choose either \"in\" or \"out\" for "
                            "this function.")

    def __converted_numpy_to_scaled_tensor(self, numpy_array,
                                           desired_dimensions=None,
                                           data_type="in"):
        """
        Transform a numpy array containing into a scaled torch tensor.

        This tensor that can simply be put into a MALA network.
        No unit conversion is done here.
        """
        numpy_array = numpy_array.astype(DEFAULT_NP_DATA_DTYPE)
        if desired_dimensions is not None:
            numpy_array = numpy_array.reshape(desired_dimensions)
        numpy_array = torch.from_numpy(numpy_array).float()
        if data_type == "in":
            self.input_data_scaler.transform(numpy_array)
        elif data_type == "out":
            self.output_data_scaler.transform(numpy_array)
        else:
            raise Exception("Please choose either \"in\" or \"out\" for "
                            "this function.")
        return numpy_array
