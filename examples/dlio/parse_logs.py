import sys, subprocess, os, json
import pprint

LOCAL_LOGS_LOCATION = "../../bin/dlio-logs"

record = {
    "pod_name": "",
    "epoch": 0,
    "scenario": "",
    "train_au_percentage": 0,
    "duration": 0,
    "train_throughput_samples_per_second": 0,
    "train_throughput_mb_per_second": 0,
    "throughput_over_local_ssd": 0,
    "start": "",
    "end": "",
    "highest_memory": 0,
    "lowest_memory": 0,
}

def get_memory(pod_name: str, start: str, end: str) -> (int, int):
    start = start.split('.')[0].replace('T', ' ') + " UTC"
    end = end.split('.')[0].replace('T', ' ') + " UTC"
    print(pod_name, start, end)
    result = subprocess.run(["mash", "--namespace=cloud_prod", "--output=csv", 
                             f"Query(Fetch(Raw('cloud.kubernetes.K8sContainer', 'kubernetes.io/container/memory/used_bytes'), {{'cluster_name': 'gcsfuse-csi-test-cluster', 'container_name': 'gke-gcsfuse-sidecar', 'pod_name': '{pod_name}', 'project': '641665282868', 'metric:memory_type': 'non-evictable'}})| Window(Align('10m'))| GroupBy(['container_name'], Sum()), TimeInterval('{start}', '{end}'), '5s')"], 
                             capture_output=True, text=True)

    data_points = result.stdout.split("\n")[1].split(",")[7:]
    data_points_int = [int(d) for d in data_points]
    if not data_points_int:
        return 0, 0
    
    return int(min(data_points_int) / 1024 / 1024) , int(max(data_points_int) / 1024 / 1024)

if __name__ == "__main__":
    bucketNames = ["gke-dlio-unet3d-100kb-500k", "gke-dlio-unet3d-150mb-5k", "gke-dlio-unet3d-3mb-100k", "gke-dlio-unet3d-500kb-1m"]
    
    try:
        os.makedirs(LOCAL_LOGS_LOCATION)
    except FileExistsError:
        pass
    
    for bucketName in bucketNames:
        print(f"Download DLIO logs from the bucket {bucketName}...")
        result = subprocess.run(["gsutil", "-m", "cp", "-r", f"gs://{bucketName}/logs", LOCAL_LOGS_LOCATION], capture_output=False, text=True)
        if result.returncode < 0:
            print(f"failed to fetch DLIO logs, error: {result.stderr}")
    
    '''
    "{num_files_train}-{mean_file_size}-{batch_size}":
        "mean_file_size": str
        "num_files_train": str
        "batch_size": str
        "records":
            "local-ssd": [record1, record2, record3, record4]
            "gcsfuse-file-cache": [record1, record2, record3, record4]
            "gcsfuse-no-file-cache": [record1, record2, record3, record4]
    '''
    output = {}

    for root, _, files in os.walk(LOCAL_LOGS_LOCATION):
        if files:
            per_epoch_stats_file = root + "/per_epoch_stats.json"
            summary_file = root + "/summary.json"
            
            with open(per_epoch_stats_file, 'r') as f:
                per_epoch_stats_data = json.load(f)
            with open(summary_file, 'r') as f:
                summary_data = json.load(f)
            
            for i in range(summary_data["epochs"]):
                test_name = summary_data["hostname"]
                part_list = test_name.split("-")
                key = "-".join(part_list[2:5])

                if key not in output:
                    output[key] = {
                        "num_files_train": part_list[2],
                        "mean_file_size": part_list[3],
                        "batch_size": part_list[4],
                        "records": {
                            "local-ssd": [],
                            "gcsfuse-file-cache": [],
                            "gcsfuse-no-file-cache": [],
                        },
                    }
                
                r = record.copy()
                r["pod_name"] = summary_data["hostname"]
                r["epoch"] = i+1
                r["scenario"] = "-".join(part_list[5:])
                r["train_au_percentage"] = round(summary_data["metric"]["train_au_percentage"][i], 2)
                r["duration"] = int(float(per_epoch_stats_data[str(i+1)]["duration"]))
                r["train_throughput_samples_per_second"] = int(summary_data["metric"]["train_throughput_samples_per_second"][i])
                r["train_throughput_mb_per_second"] = int(r["train_throughput_samples_per_second"] * int(output[key]["mean_file_size"]) / (1024 ** 2))
                r["start"] = per_epoch_stats_data[str(i+1)]["start"]
                r["end"] = per_epoch_stats_data[str(i+1)]["end"]
                r["lowest_memory"], r["highest_memory"] = get_memory(r["pod_name"], r["start"], r["end"])

                pprint.pprint(r)

                while len(output[key]["records"][r["scenario"]]) < i + 1:
                    output[key]["records"][r["scenario"]].append({})
                
                output[key]["records"][r["scenario"]][i] = r

    output_order = ["500000-102400-800", "500000-102400-128", "1000000-512000-800", "1000000-512000-128", "100000-3145728-200", "5000-157286400-4"]
    scenario_order = ["local-ssd", "gcsfuse-no-file-cache", "gcsfuse-file-cache"]

    output_file = open("./output.csv", "a")
    output_file.write("File Size,File #,Total Size (GB),Batch Size,Scenario,Epoch,Duration (s),GPU Utilization (%),Throughput (sample/s),Throughput (MB/s),Throughput over Local SSD (%),GCSFuse Lowest Memory (MB),GCSFuse Highest Memory (MB),Pod,Start,End\n")
    
    for key in output_order:
        if key not in output:
            continue
        record_set = output[key]
        total_size = int(int(record_set['mean_file_size']) * int(record_set['num_files_train']) / (1024 ** 3))

        for i in range(len(record_set["records"]["local-ssd"])):
            for scenario in scenario_order:
                r = record_set["records"][scenario][i]
                r["throughput_over_local_ssd"] = round(r["train_throughput_mb_per_second"] / record_set["records"]["local-ssd"][i]["train_throughput_mb_per_second"] * 100, 2)
                output_file.write(f"{record_set['mean_file_size']},{record_set['num_files_train']},{total_size},{record_set['batch_size']},{scenario},")
                output_file.write(f"{r['epoch']},{r['duration']},{r['train_au_percentage']},{r['train_throughput_samples_per_second']},{r['train_throughput_mb_per_second']},{r['throughput_over_local_ssd']},{r['lowest_memory']},{r['highest_memory']},{r['pod_name']},{r['start']},{r['end']}\n")

    output_file.close()