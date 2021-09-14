// standard libraries
#include <array>
#include <clocale>
#include <cstdlib>
#include <filesystem>
#include <iostream>
#include <string>

// third-party libraries
#include <nlohmann/json.hpp>

// CERN ROOT libraries
#include "TError.h"

// local libraries
#include "ParamReader.h"
#include "calibrate.h"

using Json = nlohmann::json;

// forward declarations of calibration functions
double get_position(NWBPositionCalibParamReader& nw_pcalib, int bar, double time_L, double time_R);
std::array<double, 2> get_psd(
    NWPulseShapeDiscriminationParamReader& psd_reader,
    int bar, double total_L, double total_R, double fast_L, double fast_R, double pos
);

int main(int argc, char* argv[]) {
    // initialization and argument parsing
    gErrorIgnoreLevel = kError; // ignore warnings
    const char* PROJECT_DIR = getenv("PROJECT_DIR");
    if (PROJECT_DIR == nullptr) {
        std::cerr << "Environment variable $PROJECT_DIR is not defined in current session" << std::endl;
        return 1;
    }
    std::filesystem::path project_dir(PROJECT_DIR);
    ArgumentParser argparser(argc, argv);

    // read in position calibration parameters
    NWBPositionCalibParamReader nwb_pcalib;
    nwb_pcalib.load(argparser.run_num);

    // read in pulse shape discrimination parameters
    NWPulseShapeDiscriminationParamReader nwb_psd_reader('B');
    nwb_psd_reader.load(argparser.run_num);

    // read in Daniele's ROOT files (Kuan's version)
    std::ifstream local_paths_json_file(project_dir / "database/local_paths.json");
    Json local_paths_json;
    try {
        local_paths_json_file >> local_paths_json;
        local_paths_json_file.close();
    }
    catch (...) {
        std::cerr << "Failed to read in $PROJECT_DIR/database/local_paths.json" << std::endl;
        return 1;
    }
    std::filesystem::path inroot_path = local_paths_json["daniele_root_files_dir"].get<std::string>();
    inroot_path /= Form("CalibratedData_%04d.root", argparser.run_num);
    TChain* intree = get_input_tree(inroot_path.string(), "E15190");

    // prepare output (calibrated) ROOT files
    TFile* outroot = new TFile(argparser.outroot_path.c_str(), "RECREATE");
    TTree* outtree = get_output_tree(outroot, "tree");

    // preparing progress bar
    std::setlocale(LC_NUMERIC, "");
    int total_n_entries = intree->GetEntries();
    int last_entry;
    if (argparser.n_entries < 0) {
        last_entry = total_n_entries - 1;
    }
    else {
        last_entry = std::min(total_n_entries - 1, argparser.first_entry + argparser.n_entries - 1);
    }
    int n_entries = last_entry - argparser.first_entry + 1;

    // main loop
    int ievt;
    Container& evt = container; // a shorter alias; see "calibrate.h"
    for (ievt = argparser.first_entry; ievt <= last_entry; ++ievt) {
        // progress bar
        int iprogress = ievt - argparser.first_entry;
        if (iprogress % 4321 == 0) {
            std::cout << Form("\r> %6.2f", 1e2 * iprogress / n_entries) << "%" << std::flush;
            std::cout << Form("%28s", Form("(%'d/%'d)", ievt, total_n_entries - 1)) << std::flush;
        }

        intree->GetEntry(ievt);

        // calibrations
        for (int m = 0; m < evt.NWB_multi; ++m) {
            // position calibration
            evt.NWB_pos[m] = get_position(
                nwb_pcalib,
                evt.NWB_bar[m],
                evt.NWB_time_L[m],
                evt.NWB_time_R[m]
            );

            // pulse shape discrimination
            std::array<double, 2> psd = get_psd(
                nwb_psd_reader,
                evt.NWB_bar[m],
                double(evt.NWB_total_L[m]),
                double(evt.NWB_total_R[m]),
                double(evt.NWB_fast_L[m]),
                double(evt.NWB_fast_R[m]),
                evt.NWB_pos[m]
            );
            evt.NWB_psd[m] = psd[0];
            evt.NWB_psd_perp[m] = psd[1];
        }

        outtree->Fill();
    }
    std::cout << "\r> 100.00%" << Form("%28s", Form("(%'d/%'d)", ievt - 1, total_n_entries - 1)) << std::endl;

    // save output to file
    outroot->cd();
    outtree->Write();
    outroot->Close();

    return 0;
}

double get_position(NWBPositionCalibParamReader& nw_pcalib, int bar, double time_L, double time_R) {
    int p0 = nw_pcalib.get(bar, "p0");
    int p1 = nw_pcalib.get(bar, "p1");
    double pos = p0 + p1 * (time_L - time_R);
    return pos;
}

std::array<double, 2> get_psd(
    NWPulseShapeDiscriminationParamReader& psd_reader,
    int bar,
    double total_L,
    double total_R,
    double fast_L,
    double fast_R,
    double pos
) {
    /*****eliminate bad data*****/
    if (total_L < 0 || total_R < 0 || fast_L < 0 || fast_R < 0
        || total_L > 4097 || total_R > 4097 || fast_L > 4097 || fast_R > 4097
        || pos < -120 || pos > 120
    ) {
        return {-9999.0, -9999.0};
    }

    /*****value assigning*****/
    double gamma_L = psd_reader.gamma_fast_total_L[bar]->Eval(total_L);
    double neutron_L = psd_reader.neutron_fast_total_L[bar]->Eval(total_L);
    double vpsd_L = (fast_L - gamma_L) / (neutron_L - gamma_L);

    double gamma_R = psd_reader.gamma_fast_total_R[bar]->Eval(total_R);
    double neutron_R = psd_reader.neutron_fast_total_R[bar]->Eval(total_R);
    double vpsd_R = (fast_R - gamma_R) / (neutron_R - gamma_R);

    /*****position correction*****/
    gamma_L = psd_reader.gamma_vpsd_L[bar]->Eval(pos);
    neutron_L = psd_reader.neutron_vpsd_L[bar]->Eval(pos);
    gamma_R = psd_reader.gamma_vpsd_R[bar]->Eval(pos);
    neutron_R = psd_reader.neutron_vpsd_R[bar]->Eval(pos);

    std::array<double, 2> xy = {vpsd_L - gamma_L, vpsd_R - gamma_R};
    std::array<double, 2> gn_vec = {neutron_L - gamma_L, neutron_R - gamma_R};
    std::array<double, 2> gn_rot90 = {-gn_vec[1], gn_vec[0]};

    // project to gn_vec and gn_rot90
    double x = (xy[0] * gn_vec[0] + xy[1] * gn_vec[1]);
    x /= (gn_vec[0] * gn_vec[0] + gn_vec[1] * gn_vec[1]);
    double y = (xy[0] * gn_rot90[0] + xy[1] * gn_rot90[1]);
    y /= (gn_rot90[0] * gn_rot90[0] + gn_rot90[1] * gn_rot90[1]);

    // PCA transform
    x -= psd_reader.pca_mean[bar][0];
    y -= psd_reader.pca_mean[bar][1];
    auto& pca_matrix = psd_reader.pca_components[bar];
    double pca_x = pca_matrix[0][0] * x + pca_matrix[0][1] * y;
    double pca_y = pca_matrix[1][0] * x + pca_matrix[1][1] * y;

    // normalization
    auto& xpeaks = psd_reader.pca_xpeaks[bar];
    double ppsd = (x - xpeaks[0]) / (xpeaks[1] - xpeaks[0]);
    double ppsd_perp = y;

    return {ppsd, ppsd_perp};
}