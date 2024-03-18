from collections import deque
import enum
import time
from typing import Deque, Dict, Iterable, List, Optional, Tuple, Union, Set

from vllm.config import CacheConfig, LoRAConfig, SchedulerConfig
from vllm.core.block_manager import AllocStatus, BlockSpaceManager
from vllm.core.policy import PolicyFactory
from vllm.lora.request import LoRARequest
from vllm.logger import init_logger
from vllm.sequence import (Sequence, SequenceData, SequenceGroup,
                           SequenceGroupMetadata, SequenceStatus)
from vllm.core.mlfq_function.profiling import ProfilingDatabase

logger = init_logger(__name__)


class PreemptionMode(enum.Enum):
    """Preemption modes.

    1. Swapping: Swap out the blocks of the preempted sequences to CPU memory
    and swap them back in when the sequences are resumed.
    2. Recomputation: Discard the blocks of the preempted sequences and
    recompute them when the sequences are resumed, treating the sequences as
    new prompts.
    """
    SWAP = enum.auto()
    RECOMPUTE = enum.auto()


class SchedulerOutputs:

    def __init__(
        self,
        scheduled_seq_groups: Iterable[SequenceGroup],
        prompt_run: bool,
        num_batched_tokens: int,
        blocks_to_swap_in: Dict[int, int],
        blocks_to_swap_out: Dict[int, int],
        blocks_to_copy: Dict[int, List[int]],
        ignored_seq_groups: List[SequenceGroup],
    ) -> None:
        self.scheduled_seq_groups = scheduled_seq_groups
        self.prompt_run = prompt_run
        self.num_batched_tokens = num_batched_tokens
        self.blocks_to_swap_in = blocks_to_swap_in
        self.blocks_to_swap_out = blocks_to_swap_out
        self.blocks_to_copy = blocks_to_copy
        # Swap in and swap out should never happen at the same time.
        assert not (blocks_to_swap_in and blocks_to_swap_out)
        self.ignored_seq_groups = ignored_seq_groups

        self.num_loras = len(self.lora_requests)
        if self.num_loras > 0:
            self._sort_by_lora_ids()

    def is_empty(self) -> bool:
        # NOTE: We do not consider the ignored sequence groups.
        return (not self.scheduled_seq_groups and not self.blocks_to_swap_in
                and not self.blocks_to_swap_out and not self.blocks_to_copy)

    def _sort_by_lora_ids(self) -> bool:
        self.scheduled_seq_groups = sorted(self.scheduled_seq_groups,
                                           key=lambda g:
                                           (g.lora_int_id, g.request_id))

    @property
    def lora_requests(self) -> Set[LoRARequest]:
        return {g.lora_request for g in self.scheduled_seq_groups}


class MLFQScheduler:
    
    class Priority_Queue:
        def __init__(self, priority: int):
            self.priority = priority
            self.requests = []

        def push_front(self, request) -> None:
            self.requests.insert(0, request)

        def push_back(self, request) -> None:
            self.requests.append(request)

        def pop_front(self):
            return self.requests.pop(0)
        
        def extend_front(self, requests_deque: deque) -> None:
            for request in reversed(requests_deque):
                self.push_front(request)

        def __len__(self):
            return len(self.requests)

    class Priority_Queues:
        def __init__(self):
            self.queues: List[MLFQScheduler.Priority_Queue] = []

        def add_new_queue(self, priority: int) -> None:
            if priority >= len(self.queues):
                for p in range(len(self.queues), priority + 1):
                    self.queues.append(MLFQScheduler.Priority_Queue(p))

        def pop_front(self) -> None:
            for priority in range(len(self.queues)):
                if len(self.queues[priority]) > 0:
                    return self.queues[priority].pop_front()

        def push_back(self, request) -> None:
            self.add_new_queue(request.get_priority())
            self.queues[request.get_priority()].push_back(request)

        def push_front(self, request) -> None:
            self.add_new_queue(request.get_priority())
            self.queues[request.get_priority()].push_front(request)

        def del_request(self, request_id: int) -> None:
            for queue in self.queues:
                for i, request in enumerate(queue.requests):
                    if request.request_id == request_id:
                        del queue.requests[i]
                        return

        def get_num_requests_in_top_queue(self, num_queues=2) -> int:
            for priority in range(len(self.queues)):
                if len(self.queues[priority]) > 0:
                    num_requests_in_top_queue = 0
                    for i in range(num_queues):
                        if priority + i < len(self.queues):
                            num_requests_in_top_queue += len(self.queues[priority + i])
                    return num_requests_in_top_queue
            return 0
        
        def extend_front(self, requests_deque: deque) -> None:
            for request in requests_deque:
                self.add_new_queue(request.get_priority())
                self.queues[request.get_priority()].extend_front(deque([request]))

        def __len__(self):
            return sum([len(q) for q in self.queues])

    def __init__(
        self,
        scheduler_config: SchedulerConfig,
        cache_config: CacheConfig,
        lora_config: Optional[LoRAConfig],
        proactive_offloading: bool = True,
        num_min_free_blocks_threshold: int = 0,
        num_queues_for_prediction: int = 2,
        use_skip_join: bool = False,
    ) -> None:
        self.scheduler_config = scheduler_config
        self.cache_config = cache_config
        # Note for LoRA scheduling: the current policy is extremely
        # simple and NOT fair. It can lead to starvation of some
        # LoRAs. This should be improved in the future.
        self.lora_config = lora_config
        
        self.proactive_offloading = proactive_offloading
        self.num_min_free_blocks_threshold = num_min_free_blocks_threshold
        self.num_queues_for_prediction = num_queues_for_prediction
        self.use_skip_join = use_skip_join
        self.iteration_num = 0
        # Load profiling results
        # if use_skip_join:
        #     assert (
        #         scheduler_config.profiling_file is not None
        #     ), "skip-join MLFQ needs profiling results"
        # profiling_db = ProfilingDatabase(
        #     scheduler_config.profiling_file, new_database=False
        # )
        # self.profile_res = profiling_db.results[scheduler_config.model_name]

        # Multi-level Feedback Queue
        self.waiting: self.Priority_Queues = self.Priority_Queues()
        # Since pipeline parallelism is used, there may be multiple batches under processing.
        self.cur_index = -1
        self.batch_queues = [
            [] for _ in range(1)
        ]  # List[List[Request]]

        # Just some magic numbers, need to be tuned.
        self.base_quantum = 0.01  # 10 ms
        self.threshold = 2
        
        self.starvation_threshold = 3.  # 3 seconds
        self.starvation_period = 1000  # 1000 iterations

        self.prompt_limit = min(self.scheduler_config.max_model_len,
                                self.scheduler_config.max_num_batched_tokens)

        # Instantiate the scheduling policy.
        self.policy = PolicyFactory.get_policy(policy_name="mlfq")
        # Create the block space manager.
        self.block_manager = BlockSpaceManager(
            block_size=self.cache_config.block_size,
            num_gpu_blocks=self.cache_config.num_gpu_blocks,
            num_cpu_blocks=self.cache_config.num_cpu_blocks,
            sliding_window=self.cache_config.sliding_window,
            enable_caching=self.cache_config.enable_prefix_caching)

        # Sequence groups in the RUNNING state.
        self.running: Deque[SequenceGroup] = deque()
        # Sequence groups in the SWAPPED state.
        self.swapped: self.Priority_Queues = self.Priority_Queues()

    @property
    def lora_enabled(self) -> bool:
        return bool(self.lora_config)
    
    def _profile_prompt_phrase(self, request: SequenceGroup) -> float:
        bw = (
            request.sampling_params.best_of
            if request.sampling_params.use_beam_search
            else 1
        )
        batch_size = self.scheduler_config.max_batch_size
        input_len = request.get_input_len()
        pp = 1
        tp = 1

        latency_list = self.profile_res.get_latency_list(
            pp,
            tp,
            batch_size,
            bw,
            input_len,
        )
        
        return latency_list[0]

    def add_seq_group(self, seq_group: SequenceGroup) -> None:
        if self.use_skip_join:
            prompt_time = self._profile_prompt_phrase(seq_group)
            priority = 0
            while pow(self.threshold, priority) * self.base_quantum < prompt_time:
                priority += 1
            seq_group.set_priority(priority)
            self.waiting.push_back(seq_group)
        else:
            seq_group.set_priority(0)
            self.waiting.push_back(seq_group)

    def abort_seq_group(self, request_id: Union[str, Iterable[str]]) -> None:
        """Aborts a sequence group with the given ID.

        Check if the sequence group with the given ID
            is present in any of the state queue.
        If present, remove the sequence group from the state queue.
            Also, if any of the sequences in the sequence group is not finished,
                free the sequence with status `FINISHED_ABORTED`.
        Otherwise, do nothing.

        Args:
            request_id: The ID(s) of the sequence group to abort.
        """
        if isinstance(request_id, str):
            request_id = (request_id, )
        request_ids = set(request_id)
        for state_queue in [self.running]:
            aborted_groups: List[SequenceGroup] = []
            for seq_group in state_queue:
                if not request_ids:
                    # Using 'break' here may add two extra iterations,
                    # but is acceptable to reduce complexity .
                    break
                if seq_group.request_id in request_ids:
                    # Appending aborted group into pending list.
                    aborted_groups.append(seq_group)
                    request_ids.remove(seq_group.request_id)
            for aborted_group in aborted_groups:
                # Remove the sequence group from the state queue.
                state_queue.remove(aborted_group)
                for seq in aborted_group.get_seqs():
                    if seq.is_finished():
                        continue
                    seq.status = SequenceStatus.FINISHED_ABORTED
                    self.free_seq(seq)
        for request_id in request_ids:
             self.waiting.del_request(request_id)
             self.swapped.del_request(request_id)

    def has_unfinished_seqs(self) -> bool:
        return self.waiting

    def get_num_unfinished_seq_groups(self) -> int:
        return len(self.waiting) + len(self.running) + len(self.swapped)
    
    def prevent_starvation(self, priority_queues) -> None:
        """
        Prevent starvation of the request by promoting it to the top queue.
        """
        promote_reqs = []
        cur_time = time.monotonic()
        
        for q in priority_queues.queues:
            buffer = []
            while len(q) > 0:
                request = q.pop_front()
                if cur_time - request.metrics.arrival_time  >= self.starvation_threshold:
                    promote_reqs.append(request)
                else:
                    buffer.append(request)
            
            for request in buffer:
                q.push_back(request)
        
        # promote the requests in starvation
        for request in promote_reqs:
            request.set_priority(0)
            priority_queues.push_front(request)

    def _schedule(self) -> SchedulerOutputs:
        # Blocks that need to be swapped or copied before model execution.
        blocks_to_swap_in: Dict[int, int] = {}
        blocks_to_swap_out: Dict[int, int] = {}
        blocks_to_copy: Dict[int, List[int]] = {}

        # Fix the current time.
        now = time.monotonic()
        
        def waiting_get_heigher_priorty(self):
            if len(self.swapped) and len(self.waiting):
                swapped_first_seq_group = self.swapped.pop_front()
                waiting_first_seq_group = self.waiting.pop_front()
                swapped_first_seq_group_priority = swapped_first_seq_group.get_priority()
                waiting_first_seq_group_priority = waiting_first_seq_group.get_priority()
                swapped_first_seq_group_arr_time = swapped_first_seq_group.metrics.arrival_time
                waiting_first_seq_group_arr_time = waiting_first_seq_group.metrics.arrival_time
                self.swapped.push_front(swapped_first_seq_group)
                self.waiting.push_front(waiting_first_seq_group)
                return (waiting_first_seq_group_priority >= swapped_first_seq_group_priority) and (waiting_first_seq_group_arr_time <= swapped_first_seq_group_arr_time)
            return False

        # Join waiting sequences if possible.
        if  len(self.swapped) == 0 or waiting_get_heigher_priorty(self):
            ignored_seq_groups: List[SequenceGroup] = []
            scheduled: List[SequenceGroup] = []
            # The total number of sequences on the fly, including the
            # requests in the generation phase.
            num_curr_seqs = sum(seq_group.get_max_num_running_seqs()
                                for seq_group in self.running)
            curr_loras = set(
                seq_group.lora_int_id
                for seq_group in self.running) if self.lora_enabled else None
            seq_lens: List[int] = []

            # Optimization: We do not sort the waiting queue since the preempted
            # sequence groups are added to the front and the new sequence groups
            # are added to the back.
            leftover_waiting_sequences = deque()
            while len(self.waiting):
                seq_group = self.waiting.pop_front()
                waiting_seqs = seq_group.get_seqs(
                    status=SequenceStatus.WAITING)
                assert len(waiting_seqs) == 1, (
                    "Waiting sequence group should have only one prompt "
                    "sequence.")
                num_prompt_tokens = waiting_seqs[0].get_len()
                if num_prompt_tokens > self.prompt_limit:
                    logger.warning(
                        f"Input prompt ({num_prompt_tokens} tokens) is too long"
                        f" and exceeds limit of {self.prompt_limit}")
                    for seq in waiting_seqs:
                        seq.status = SequenceStatus.FINISHED_IGNORED
                    ignored_seq_groups.append(seq_group)
                    continue

                # If the sequence group cannot be allocated, stop.
                can_allocate = self.block_manager.can_allocate(seq_group)
                if can_allocate == AllocStatus.LATER:
                    self.waiting.push_front(seq_group)
                    break
                elif can_allocate == AllocStatus.NEVER:
                    logger.warning(
                        f"Input prompt ({num_prompt_tokens} tokens) is too long"
                        f" and exceeds the capacity of block_manager")
                    for seq in waiting_seqs:
                        seq.status = SequenceStatus.FINISHED_IGNORED
                    ignored_seq_groups.append(seq_group)
                    continue

                lora_int_id = 0
                if self.lora_enabled:
                    lora_int_id = seq_group.lora_int_id
                    if (lora_int_id > 0 and lora_int_id not in curr_loras
                            and len(curr_loras) >= self.lora_config.max_loras):
                        # We don't have a space for another LoRA, so
                        # we ignore this request for now.
                        leftover_waiting_sequences.appendleft(seq_group)
                        continue

                # If the number of batched tokens exceeds the limit, stop.
                new_seq_lens = seq_lens + [num_prompt_tokens]
                num_batched_tokens = len(new_seq_lens) * max(new_seq_lens)
                if (num_batched_tokens >
                        self.scheduler_config.max_num_batched_tokens):
                    self.waiting.push_front(seq_group)
                    break

                # The total number of sequences in the RUNNING state should not
                # exceed the maximum number of sequences.
                num_new_seqs = seq_group.get_max_num_running_seqs()
                if (num_curr_seqs + num_new_seqs >
                        self.scheduler_config.max_num_seqs):
                    self.waiting.push_front(seq_group)
                    break

                num_paddings = num_batched_tokens - sum(new_seq_lens)
                if num_paddings > self.scheduler_config.max_paddings:
                    self.waiting.push_front(seq_group)
                    break
                seq_lens = new_seq_lens

                if lora_int_id > 0:
                    curr_loras.add(lora_int_id)
                self._allocate(seq_group)
                self.running.append(seq_group)
                num_curr_seqs += num_new_seqs
                scheduled.append(seq_group)

            self.waiting.extend_front(leftover_waiting_sequences)
            
            self.iteration_num += 1
            
            if self.iteration_num % self.starvation_period == 0:
                self.prevent_starvation(self.waiting)
                self.prevent_starvation(self.swapped)
    
            if scheduled or ignored_seq_groups:
                scheduler_outputs = SchedulerOutputs(
                    scheduled_seq_groups=scheduled,
                    prompt_run=True,
                    num_batched_tokens=len(seq_lens) *
                    max(seq_lens) if seq_lens else 0,
                    blocks_to_swap_in=blocks_to_swap_in,
                    blocks_to_swap_out=blocks_to_swap_out,
                    blocks_to_copy=blocks_to_copy,
                    ignored_seq_groups=ignored_seq_groups,
                )
                return scheduler_outputs

        # NOTE(woosuk): Preemption happens only when there is no available slot
        # to keep all the sequence groups in the RUNNING state.
        # In this case, the policy is responsible for deciding which sequence
        # groups to preempt.
        self.running = self.policy.sort_by_priority(now, self.running)

        # Reserve new token slots for the running sequence groups.
        running: Deque[SequenceGroup] = deque()
        preempted: List[SequenceGroup] = []
        while self.running:
            seq_group = self.running.popleft()
            while not self.block_manager.can_append_slot(seq_group):
                if self.running:
                    # Preempt the lowest-priority sequence groups.
                    victim_seq_group = self.running.pop()
                    self._preempt(victim_seq_group, blocks_to_swap_out, PreemptionMode.SWAP)
                    preempted.append(victim_seq_group)
                else:
                    # No other sequence groups can be preempted.
                    # Preempt the current sequence group.
                    self._preempt(seq_group, blocks_to_swap_out, PreemptionMode.SWAP)
                    preempted.append(seq_group)
                    break
            else:
                # Append new slots to the sequence group.
                self._append_slot(seq_group, blocks_to_copy)
                running.append(seq_group)
        self.running = running

        # Swap in the sequence groups in the SWAPPED state if possible.
        # self.swapped = self.policy.sort_by_priority(now, self.swapped)
        if not preempted:
            num_curr_seqs = sum(seq_group.get_max_num_running_seqs()
                                for seq_group in self.running)
            curr_loras = set(
                seq_group.lora_int_id
                for seq_group in self.running) if self.lora_enabled else None

            leftover_swapped = deque()

            while len(self.swapped):
                seq_group = self.swapped.pop_front()
                lora_int_id = 0
                if self.lora_enabled:
                    lora_int_id = seq_group.lora_int_id
                    if (lora_int_id > 0 and lora_int_id not in curr_loras
                            and len(curr_loras) >= self.lora_config.max_loras):
                        # We don't have a space for another LoRA, so
                        # we ignore this request for now.
                        leftover_swapped.appendleft(seq_group)
                        continue

                # If the sequence group cannot be swapped in, stop.
                if not self.block_manager.can_swap_in(seq_group):
                    self.swapped.push_front(seq_group)
                    break

                # The total number of sequences in the RUNNING state should not
                # exceed the maximum number of sequences.
                num_new_seqs = seq_group.get_max_num_running_seqs()
                if (num_curr_seqs + num_new_seqs >
                        self.scheduler_config.max_num_seqs):
                    self.swapped.push_front(seq_group)
                    break

                if lora_int_id > 0:
                    curr_loras.add(lora_int_id)
                self._swap_in(seq_group, blocks_to_swap_in)
                self._append_slot(seq_group, blocks_to_copy)
                num_curr_seqs += num_new_seqs
                self.running.append(seq_group)

            self.swapped.extend_front(leftover_swapped)
            
        self.iteration_num += 1
            
        if self.iteration_num % self.starvation_period == 0:
            self.prevent_starvation(self.waiting)
            self.prevent_starvation(self.swapped)

        # Each sequence in the generation phase only takes one token slot.
        # Therefore, the number of batched tokens is equal to the number of
        # sequences in the RUNNING state.
        num_batched_tokens = sum(
            seq_group.num_seqs(status=SequenceStatus.RUNNING)
            for seq_group in self.running)

        scheduler_outputs = SchedulerOutputs(
            scheduled_seq_groups=self.running,
            prompt_run=False,
            num_batched_tokens=num_batched_tokens,
            blocks_to_swap_in=blocks_to_swap_in,
            blocks_to_swap_out=blocks_to_swap_out,
            blocks_to_copy=blocks_to_copy,
            ignored_seq_groups=[],
        )
        return scheduler_outputs

    def schedule(self) -> Tuple[List[SequenceGroupMetadata], SchedulerOutputs]:
        # Schedule sequence groups.
        # This function call changes the internal states of the scheduler
        # such as self.running, self.swapped, and self.waiting.
        scheduler_outputs = self._schedule()
        now = time.time()

        # Create input data structures.
        seq_group_metadata_list: List[SequenceGroupMetadata] = []
        for seq_group in scheduler_outputs.scheduled_seq_groups:
            seq_group.maybe_set_first_scheduled_time(now)

            seq_data: Dict[int, SequenceData] = {}
            block_tables: Dict[int, List[int]] = {}

            for seq in seq_group.get_seqs(status=SequenceStatus.RUNNING):
                seq_id = seq.seq_id
                seq_data[seq_id] = seq.data
                block_tables[seq_id] = self.block_manager.get_block_table(seq)
                self.block_manager.access_all_blocks_in_seq(seq, now)

            seq_group_metadata = SequenceGroupMetadata(
                request_id=seq_group.request_id,
                is_prompt=scheduler_outputs.prompt_run,
                seq_data=seq_data,
                sampling_params=seq_group.sampling_params,
                block_tables=block_tables,
                lora_request=seq_group.lora_request,
                computed_block_nums=self.block_manager.
                get_common_computed_block_ids(seq_group),
                state=seq_group.state,
            )
            seq_group_metadata_list.append(seq_group_metadata)
        return seq_group_metadata_list, scheduler_outputs

    def fork_seq(self, parent_seq: Sequence, child_seq: Sequence) -> None:
        self.block_manager.fork(parent_seq, child_seq)

    def free_seq(self, seq: Sequence) -> None:
        self.block_manager.free(seq)

    def free_finished_seq_groups(self) -> None:
        temp_running = deque()
        for seq_group in self.running:
            if not seq_group.is_finished():
                 # put the request back to mlfq and try to demote it
                current_time = time.monotonic()
                if (current_time - seq_group.metrics.arrival_time) > self.base_quantum * pow(
                    self.threshold, seq_group.get_priority()
                ):
                    seq_group.set_priority(seq_group.get_priority() + 1)
                    seq_group.metrics.arrival_time =  current_time
                    self.swapped.push_front(seq_group)
                    continue
                else:           
                    temp_running.append(seq_group)
        self.running = temp_running
        

    def _allocate(self, seq_group: SequenceGroup) -> None:
        self.block_manager.allocate(seq_group)
        for seq in seq_group.get_seqs(status=SequenceStatus.WAITING):
            seq.status = SequenceStatus.RUNNING

    def _append_slot(
        self,
        seq_group: SequenceGroup,
        blocks_to_copy: Dict[int, List[int]],
    ) -> None:
        for seq in seq_group.get_seqs(status=SequenceStatus.RUNNING):
            ret = self.block_manager.append_slot(seq)
            if ret is not None:
                src_block, dst_block = ret
                if src_block in blocks_to_copy:
                    blocks_to_copy[src_block].append(dst_block)
                else:
                    blocks_to_copy[src_block] = [dst_block]

    def _preempt(
        self,
        seq_group: SequenceGroup,
        blocks_to_swap_out: Dict[int, int],
        preemption_mode: Optional[PreemptionMode] = None,
    ) -> None:
        # If preemption mode is not specified, we determine the mode as follows:
        # We use recomputation by default since it incurs lower overhead than
        # swapping. However, when the sequence group has multiple sequences
        # (e.g., beam search), recomputation is not currently supported. In
        # such a case, we use swapping instead.
        # FIXME(woosuk): This makes our scheduling policy a bit bizarre.
        # As swapped sequences are prioritized over waiting sequences,
        # sequence groups with multiple sequences are implicitly prioritized
        # over sequence groups with a single sequence.
        # TODO(woosuk): Support recomputation for sequence groups with multiple
        # sequences. This may require a more sophisticated CUDA kernel.
        if preemption_mode is None:
            if seq_group.get_max_num_running_seqs() == 1:
                preemption_mode = PreemptionMode.RECOMPUTE
            else:
                preemption_mode = PreemptionMode.SWAP
        if preemption_mode == PreemptionMode.RECOMPUTE:
            self._preempt_by_recompute(seq_group)
        elif preemption_mode == PreemptionMode.SWAP:
            self._preempt_by_swap(seq_group, blocks_to_swap_out)
        else:
            raise AssertionError("Invalid preemption mode.")

    def _preempt_by_recompute(
        self,
        seq_group: SequenceGroup,
    ) -> None:
        seqs = seq_group.get_seqs(status=SequenceStatus.RUNNING)
        assert len(seqs) == 1
        for seq in seqs:
            seq.status = SequenceStatus.WAITING
            self.block_manager.free(seq)
        # NOTE: For FCFS, we insert the preempted sequence group to the front
        # of the waiting queue.
        self.waiting.push_front(seq_group)

    def _preempt_by_swap(
        self,
        seq_group: SequenceGroup,
        blocks_to_swap_out: Dict[int, int],
    ) -> None:
        self._swap_out(seq_group, blocks_to_swap_out)
        self.swapped.push_back(seq_group)

    def _swap_in(
        self,
        seq_group: SequenceGroup,
        blocks_to_swap_in: Dict[int, int],
    ) -> None:
        mapping = self.block_manager.swap_in(seq_group)
        blocks_to_swap_in.update(mapping)
        for seq in seq_group.get_seqs(status=SequenceStatus.SWAPPED):
            seq.status = SequenceStatus.RUNNING

    def _swap_out(
        self,
        seq_group: SequenceGroup,
        blocks_to_swap_out: Dict[int, int],
    ) -> None:
        if not self.block_manager.can_swap_out(seq_group):
            # FIXME(woosuk): Abort the sequence group instead of aborting the
            # entire engine.
            raise RuntimeError(
                "Aborted due to the lack of CPU swap space. Please increase "
                "the swap space to avoid this error.")
        mapping = self.block_manager.swap_out(seq_group)
        blocks_to_swap_out.update(mapping)
        for seq in seq_group.get_seqs(status=SequenceStatus.RUNNING):
            seq.status = SequenceStatus.SWAPPED

    def mark_blocks_as_computed(self, seq_group: SequenceGroup):
        self.block_manager.mark_blocks_as_computed(seq_group)
