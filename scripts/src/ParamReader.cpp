#include <algorithm>
#include <cctype>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <sstream>
#include <string>
#include <unistd.h>
#include <unordered_map>
#include <vector>

#include <nlohmann/json.hpp>

#include "TFolder.h"
#include "Math/Interpolator.h"
#include "Math/InterpolationTypes.h"
#include "TNamed.h"
#include "TTreeReaderValue.h"

#include "ParamReader.h"

using Json = nlohmann::json;

/************************************/
/*****NWPositionCalibParamReader*****/
/************************************/
NWPositionCalibParamReader::NWPositionCalibParamReader(const char AB) : AB(AB), ab(tolower(AB)) {
    this->pcalib_filepath = this->resolve_project_dir(this->pcalib_filepath).string();
    this->pca_filepath = this->resolve_project_dir(this->pca_filepath).string();
}

NWPositionCalibParamReader::~NWPositionCalibParamReader() { }

std::filesystem::path NWPositionCalibParamReader::resolve_project_dir(const std::string& path_str) {
    std::filesystem::path path(path_str);
    const char* project_dir = std::getenv("PROJECT_DIR");
    if (!project_dir) {
        return path;
    }

    std::size_t pos = path.string().find("$PROJECT_DIR");
    if (pos != std::string::npos) {
        std::string newPath = path.string();
        newPath.replace(pos, strlen("$PROJECT_DIR"), project_dir);
        newPath = Form(newPath.c_str(), this->AB);
        path = std::filesystem::path(newPath);
    }

    return path;
}

bool NWPositionCalibParamReader::load(int run) {
    std::ifstream pcalib_file(this->pcalib_filepath);
    if (!pcalib_file.is_open()) {
        std::cerr << "Error: Could not open the position calibration parameters file: " << this->pcalib_filepath << std::endl;
        return false;
    }

    Json json_data;
    pcalib_file >> json_data;
    pcalib_file.close();

    for (const auto& [bar, bar_entry] : json_data.items()) {
        int b = std::stoi(bar);
        for (const auto& run_range_entry : bar_entry) {
            int run_start = run_range_entry["run_range"][0].get<int>();
            int run_end = run_range_entry["run_range"][1].get<int>();

            if (run >= run_start && run <= run_end) {
                this->param[{b, "p0"}] = run_range_entry["parameters"][0].get<double>();
                this->param[{b, "p1"}] = run_range_entry["parameters"][1].get<double>();
                break;
            }
        }
    }


    std::ifstream pca_file(this->pca_filepath);
    if (!pca_file.is_open()) {
        std::cerr << "Error: Could not open the PCA parameters file: " << this->pca_filepath << std::endl;
        return false;
    }

    std::string line;
    while (std::getline(pca_file, line)) {
        if (line.empty() || line[0] == '#') {
            continue;  // skip empty or comment lines
        }

        std::istringstream iss(line);
        int bar;
        std::string vec;
        double x, y, z;
        iss >> bar >> vec >> x >> y >> z;
        this->param[{bar, vec + "0"}] = x;
        this->param[{bar, vec + "1"}] = y;
        this->param[{bar, vec + "2"}] = z;
    }

    return true;
}

double NWPositionCalibParamReader::get(int bar, const std::string& par) {
    return this->param[{bar, par}];
}

void NWPositionCalibParamReader::write_metadata(TFolder* folder, bool relative_path) {
    std::filesystem::path base_dir = (relative_path) ? this->resolve_project_dir("$PROJECT_DIR") : "/";
    std::filesystem::path path;

    path = std::filesystem::proximate(std::filesystem::path(this->pcalib_filepath), base_dir);
    TNamed* json_path_data = new TNamed(path.string().c_str(), "");
    folder->Add(json_path_data);

    path = std::filesystem::proximate(std::filesystem::path(this->pca_filepath), base_dir);
    TNamed* pca_path_data = new TNamed(path.string().c_str(), "");
    folder->Add(pca_path_data);

    return;
}



/****************************************/
/*****NWTimeOfFlightCalibParamReader*****/
/****************************************/
NWTimeOfFlightCalibParamReader::NWTimeOfFlightCalibParamReader(const char AB, bool load_params) {
    const char* PROJECT_DIR = getenv("PROJECT_DIR");
    if (PROJECT_DIR == nullptr) {
        std::cerr << "Environment variable $PROJECT_DIR is not defined in current session" << std::endl;
        exit(1);
    }
    this->AB = toupper(AB);
    this->ab = tolower(this->AB);
    this->project_dir = PROJECT_DIR;
    this->calib_dir = this->project_dir / this->calib_dir;
    this->json_path = this->calib_dir / Form(this->json_filename.c_str(), this->ab);

    if (load_params) {
        this->load_tof_offset();
    }
}

NWTimeOfFlightCalibParamReader::~NWTimeOfFlightCalibParamReader() { }

void NWTimeOfFlightCalibParamReader::load_tof_offset() {
    /* Read in all TOF offset parameters from .json file to this->database */
    std::ifstream file(this->json_path.string());
    if (!file.is_open()) {
        std::cerr << "ERROR: failed to open " << this->json_path.string() << std::endl;
        exit(1);
    }
    this->database.clear();
    file >> this->database;
    file.close();
}

void NWTimeOfFlightCalibParamReader::load(int run) {
    /* Load TOF offset parameters for a given run
     * from this->database to this->tof_offset.
     */
    for (auto& [bar, bar_info] : this->database.items()) {
        bool found = false;
        for (auto& par_info : bar_info) {
            auto& run_range = par_info["run_range"];
            if (run < run_range[0].get<int>() || run > run_range[1].get<int>()) {
                continue;
            }
            this->tof_offset[std::stoi(bar)] = par_info["tof_offset"].get<double>();
            found = true;
            break;
        }
        if (!found) {
            std::cerr << Form(
                "ERROR: run-%04d is not found for NW%c bar%02d",
                run, this->AB, std::stoi(bar)
            ) << std::endl;
        }
    }
}

void NWTimeOfFlightCalibParamReader::write_metadata(TFolder* folder, bool relative_path) {
    std::filesystem::path base_dir = (relative_path) ? this->project_dir : "/";
    std::filesystem::path path;

    path = std::filesystem::proximate(this->json_path, base_dir);
    TNamed* path_data = new TNamed(path.string().c_str(), "");
    folder->Add(path_data);
}


/**************************************/
/*****NWADCPreprocessorParamReader*****/
/**************************************/
NWADCPreprocessorParamReader::NWADCPreprocessorParamReader(const char AB) {
    const char* PROJECT_DIR = getenv("PROJECT_DIR");
    if (PROJECT_DIR == nullptr) {
        std::cerr << "Environment variable $PROJECT_DIR is not defined in current session" << std::endl;
        exit(1);
    }
    this->AB = toupper(AB);
    this->ab = tolower(this->AB);
    this->project_dir = PROJECT_DIR;
}

NWADCPreprocessorParamReader::~NWADCPreprocessorParamReader() { }

void NWADCPreprocessorParamReader::load(int run) {
    this->run = run;
    this->calib_reldir = this->project_dir / Form(this->calib_reldir.string().c_str(), this->run);
    this->load_fast_total('L');
    this->load_fast_total('R');
    this->load_log_ratio_total();
    return;
}

void NWADCPreprocessorParamReader::load_fast_total(char side) {
    auto filepath = this->calib_reldir / Form(this->filename.c_str(), Form("fast_total_%c", side));
    this->filepaths.push_back(filepath);
    std::ifstream file(filepath.string());
    if (!file.is_open()) {
        std::cerr << "ERROR: failed to open " << filepath.string() << std::endl;
        exit(1);
    }
    Json content;
    file >> content;
    file.close();

    auto& map = (side == 'L') ? this->fast_total_L : this->fast_total_R;
    for (int bar = 1; bar <= 24; ++bar) {
        Json& bar_content = content[std::to_string(bar)];
        Json info;
        for (auto& chunk : bar_content) {
            if (chunk["run_range"][0].get<int>() <= this->run && this->run <= chunk["run_range"][1].get<int>()) {
                info = chunk;
                break;
            }
        }
        if (info.is_null()) {
            std::cerr << Form("ERROR: run-%04d is not found for NW%c bar%02d", this->run, this->AB, bar) << std::endl;
            exit(1);
        }
        map[bar] = {
            {"nonlinear_fast_threshold", info["nonlinear_fast_threshold"].get<double>()},
            {"stationary_point_x", info["stationary_point_x"].get<double>()},
            {"stationary_point_y", info["stationary_point_y"].get<double>()},
            {"fit_params[0]", info["linear_fit_params"][0].get<double>() - info["quadratic_fit_params"][0].get<double>()},
            {"fit_params[1]", info["linear_fit_params"][1].get<double>() - info["quadratic_fit_params"][1].get<double>()},
            {"fit_params[2]", -info["quadratic_fit_params"][2].get<double>()},
        };
    }
    return;
}

void NWADCPreprocessorParamReader::load_log_ratio_total() {
    auto filepath = this->calib_reldir / Form(this->filename.c_str(), "log_ratio_total");
    this->filepaths.push_back(filepath);
    std::ifstream file(filepath.string());
    if (!file.is_open()) {
        std::cerr << "ERROR: failed to open " << filepath.string() << std::endl;
        exit(1);
    }
    Json content;
    file >> content;
    file.close();

    auto& map = this->log_ratio_total;
    for (int bar = 1; bar <= 24; ++bar) {
        Json& bar_content = content[std::to_string(bar)];
        Json info;
        for (auto& chunk : bar_content) {
            if (chunk["run_range"][0].get<int>() <= this->run && this->run <= chunk["run_range"][1].get<int>()) {
                info = chunk;
                break;
            }
        }
        if (info.is_null()) {
            std::cerr << Form("ERROR: run-%04d is not found for NW%c bar%02d", this->run, this->AB, bar) << std::endl;
            exit(1);
        }
        map[bar] = {
            {"attenuation_length", info["attenuation_length"].get<double>()},
            {"gain_ratio", info["gain_ratio"].get<double>()},
        };
    }
    return;
}

void NWADCPreprocessorParamReader::write_metadata(TFolder* folder, bool relative_path) {
    std::filesystem::path base_dir = (relative_path) ? this->project_dir : "/";
    std::filesystem::path path;
    for (auto& filepath : this->filepaths) {
        path = std::filesystem::proximate(filepath, base_dir);
        TNamed* named = new TNamed(path.string().c_str(), "");
        folder->Add(named);
    }
    return;
}


/***************************************/
/*****NWLightOutputCalibParamReader*****/
/***************************************/
NWLightOutputCalibParamReader::NWLightOutputCalibParamReader(const char AB) {
    const char* PROJECT_DIR = getenv("PROJECT_DIR");
    if (PROJECT_DIR == nullptr) {
        std::cerr << "Environment variable $PROJECT_DIR is not defined in current session" << std::endl;
        exit(1);
    }
    this->AB = toupper(AB);
    this->ab = tolower(this->AB);
    this->project_dir = PROJECT_DIR;
    this->lcalib_reldir = this->project_dir / this->lcalib_reldir;
    this->pul_path = this->lcalib_reldir / Form(this->pul_filename.c_str(), this->ab);
}

NWLightOutputCalibParamReader::~NWLightOutputCalibParamReader() { }

void NWLightOutputCalibParamReader::load_pulse_height() {
    std::vector<std::string> keys = {"a", "b", "c", "d", "e"};
    std::ifstream infile(this->pul_path.c_str());
    std::string line;
    std::getline(infile, line);
    while (!infile.eof()) {
        std::getline(infile, line);
        if (line.empty()) { continue; }

        std::stringstream ss(line);
        int bar;
        ss >> bar;
        for (std::string& key: keys) {
            ss >> this->run_param[bar][key];
        }
    }
    infile.close();
}

void NWLightOutputCalibParamReader::load(int run) {
    // run dependency not implemented
    this->load_pulse_height();
    return;
}

void NWLightOutputCalibParamReader::write_metadata(TFolder* folder, bool relative_path) {
    std::filesystem::path base_dir = (relative_path) ? this->project_dir : "/";
    std::filesystem::path path = std::filesystem::proximate(this->pul_path, base_dir);
    TNamed* pul_path_data = new TNamed(path.string().c_str(), "");
    folder->Add(pul_path_data);

    return;
}



/***********************************************/
/*****NWPulseShapeDiscriminationParamReader*****/
/***********************************************/
double NWPulseShapeDiscriminationParamReader::polynomial(double x, std::vector<double>& params) {
    double y = 0;
    for (int i = 0; i < params.size(); ++i) {
        y += params[i] * std::pow(x, i);
    }
    return y;
}

double NWPulseShapeDiscriminationParamReader::polynomial(double x, Json& params) {
    std::vector<double> params_vec;
    for (auto& param : params) {
        params_vec.push_back(param.get<double>());
    }
    return this->polynomial(x, params_vec);
}

std::vector<double> NWPulseShapeDiscriminationParamReader::get_neutron_linear_params(double x_switch_neutron, std::vector<double>& quadratic_params) {
    auto& quad = quadratic_params;
    double lin1 = quad[1] + 2 * quad[2] * x_switch_neutron;
    double lin0 = quad[0] + quad[1] * x_switch_neutron + quad[2] * std::pow(x_switch_neutron, 2) - lin1 * x_switch_neutron;
    return {lin0, lin1};
}

std::vector<double> NWPulseShapeDiscriminationParamReader::get_neutron_linear_params(double x_switch_neutron, Json& quadratic_params) {
    std::vector<double> quadratic_params_vec;
    for (auto& param : quadratic_params) {
        quadratic_params_vec.push_back(param.get<double>());
    }
    return this->get_neutron_linear_params(x_switch_neutron, quadratic_params_vec);
}

Json NWPulseShapeDiscriminationParamReader::get_bar_params(int run, int bar) {
    auto& bar_params = this->database[Form("%d", bar)];
    Json params;
    for (auto& run_range_params : bar_params) {
        auto& run_range = run_range_params["run_range"];
        int run_start = (int)run_range[0].get<double>();
        int run_stop = (int)run_range[1].get<double>();
        if (run >= run_start && run <= run_stop) {
            params = run_range_params;
            break;
        }
    }
    if (params.empty()) {
        std::cerr << Form("Cannot find run %04d for NW%c-bar%02d", run, this->AB, bar) << std::endl;
        exit(1);
    }
    return params;
}

void NWPulseShapeDiscriminationParamReader::fast_total_interpolation(int bar, Json& params) {
    auto method = ROOT::Math::Interpolation::kAKIMA;

    std::vector<double> totals;
    for (double total = -20.0; total <= 4020.0; total += 20.0) {
        totals.push_back(total);
    }
    std::vector<double> fasts;

    fasts.clear();
    for (int i = 0; i < totals.size(); ++i) {
        double fast = this->polynomial(totals[i], params["cline_L"]) + this->polynomial(totals[i], params["g_cfast_L"]);
        fasts.push_back(fast);
    }
    this->gamma_fast_total_L[bar] = new ROOT::Math::Interpolator(totals, fasts, method);

    fasts.clear();
    for (int i = 0; i < totals.size(); ++i) {
        auto& _params = params["n_cfast_L"];
        if (totals[i] >= params["x_switch_neutron"]) {
            _params = this->get_neutron_linear_params(params["x_switch_neutron"], _params);
        }
        double fast = this->polynomial(totals[i], params["cline_L"]) + this->polynomial(totals[i], _params);
        fasts.push_back(fast);
    }
    this->neutron_fast_total_L[bar] = new ROOT::Math::Interpolator(totals, fasts, method);

    fasts.clear();
    for (int i = 0; i < totals.size(); ++i) {
        double fast = this->polynomial(totals[i], params["cline_R"]) + this->polynomial(totals[i], params["g_cfast_R"]);
        fasts.push_back(fast);
    }
    this->gamma_fast_total_R[bar] = new ROOT::Math::Interpolator(totals, fasts, method);

    fasts.clear();
    for (int i = 0; i < totals.size(); ++i) {
        auto& _params = params["n_cfast_R"];
        if (totals[i] >= params["x_switch_neutron"]) {
            _params = this->get_neutron_linear_params(params["x_switch_neutron"], _params);
        }
        double fast = this->polynomial(totals[i], params["cline_R"]) + this->polynomial(totals[i], _params);
        fasts.push_back(fast);
    }
    this->neutron_fast_total_R[bar] = new ROOT::Math::Interpolator(totals, fasts, method);
}

void NWPulseShapeDiscriminationParamReader::centroid_interpolation(int bar, Json& params) {
    auto method = ROOT::Math::Interpolation::kAKIMA;

    std::vector<double> pos_x;
    for (auto& x: params["centroid_pos_x"]) {
        pos_x.push_back(x.get<double>());
    }
    std::vector<double> coords;

    coords.clear();
    for (int i = 0; i < pos_x.size(); ++i) {
        coords.push_back(params["g_centroid_L"][i].get<double>());
    }
    this->gamma_vpsd_L[bar] = new ROOT::Math::Interpolator(pos_x, coords, method);

    coords.clear();
    for (int i = 0; i < pos_x.size(); ++i) {
        coords.push_back(params["n_centroid_L"][i].get<double>());
    }
    this->neutron_vpsd_L[bar] = new ROOT::Math::Interpolator(pos_x, coords, method);

    coords.clear();
    for (int i = 0; i < pos_x.size(); ++i) {
        coords.push_back(params["g_centroid_R"][i].get<double>());
    }
    this->gamma_vpsd_R[bar] = new ROOT::Math::Interpolator(pos_x, coords, method);

    coords.clear();
    for (int i = 0; i < pos_x.size(); ++i) {
        coords.push_back(params["n_centroid_R"][i].get<double>());
    }
    this->neutron_vpsd_R[bar] = new ROOT::Math::Interpolator(pos_x, coords, method);
}

void NWPulseShapeDiscriminationParamReader::process_pca(int bar, Json& params) {
    this->pca_mean[bar] = {params["pca_mean"][0].get<double>(), params["pca_mean"][1].get<double>()};
    this->pca_components[bar][0] = {params["pca_components"][0][0].get<double>(), params["pca_components"][0][1].get<double>()};
    this->pca_components[bar][1] = {params["pca_components"][1][0].get<double>(), params["pca_components"][1][1].get<double>()};
    this->pca_xpeaks[bar] = {params["pca_xpeaks"][0].get<double>(), params["pca_xpeaks"][1].get<double>()};
}

NWPulseShapeDiscriminationParamReader::NWPulseShapeDiscriminationParamReader(const char AB) {
    this->AB = toupper(AB);
    this->ab = tolower(this->AB);

    for (int bar = 1; bar <= 24; ++bar) {
        this->bars.push_back(bar);
    }

    const char* PROJECT_DIR = getenv("PROJECT_DIR");
    if (PROJECT_DIR == nullptr) {
        std::cerr << "Environment variable $PROJECT_DIR is not defined in current session" << std::endl;
        exit(1);
    }
    this->project_dir = std::filesystem::path(PROJECT_DIR);
    this->param_dir = this->project_dir / this->param_reldir;
}

NWPulseShapeDiscriminationParamReader::~NWPulseShapeDiscriminationParamReader() { }

void NWPulseShapeDiscriminationParamReader::read_in_calib_params() {
    this->param_path = this->param_dir / Form("calib_params_nw%c.json", this->ab);
    std::ifstream database_file(this->param_path.string());
    if (!database_file.is_open()) {
        std::cerr << "Failed to open database file: " << this->param_path << std::endl;
        exit(1);
    }
    database_file >> this->database;
    database_file.close();
}

void NWPulseShapeDiscriminationParamReader::load(int run) {
    this->read_in_calib_params();
    for (int bar: this->bars) {
        auto params = this->get_bar_params(run, bar);
        this->fast_total_interpolation(bar, params);
        this->centroid_interpolation(bar, params);
        this->process_pca(bar, params);
    }
}

void NWPulseShapeDiscriminationParamReader::write_metadata(TFolder* folder, bool relative_path) {
    std::filesystem::path base_dir = (relative_path) ? this->project_dir : "/";
    std::filesystem::path path = std::filesystem::proximate(this->param_path, base_dir);
    TNamed* data = new TNamed(path.string().c_str(), "PulseShapeDiscrimination_param_path");
    folder->Add(data);
    return;
}
