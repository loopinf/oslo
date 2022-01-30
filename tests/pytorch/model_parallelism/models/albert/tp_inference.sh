# USAGE:   ``sh ./tp_inference.sh $NUM_GPUS``
# EXAMPLE: ``sh ./tp_inference.sh 4``

NUM_GPUS=$1

python -m torch.distributed.launch \
       --nproc_per_node="$NUM_GPUS" \
       ../../testcases/inference.py \
       --task=masked-lm \
       --model=albert-base-v2 \
       --tensor_parallel_size="$NUM_GPUS"

python -m torch.distributed.launch \
       --nproc_per_node="$NUM_GPUS" \
       ../../testcases/inference.py \
       --task=sequence-classification \
       --model=Alireza1044/albert-base-v2-sst2 \
       --tensor_parallel_size="$NUM_GPUS"
