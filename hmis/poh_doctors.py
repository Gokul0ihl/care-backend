"""Pacific OneHealth's consultants, as listed on pacificonehealth.com (November 2026).

The source for seed_poh_doctors. Edit here to add, change or move a doctor, then run
the command again. "photo" is the image URL on the POH website (None: initials show);
each posting is (facility name, department, designation), and the department is the
website's specialty name. A doctor at several centres has one posting per centre.
"""

POH_DOCTORS = [
    {
        "name": "Dr. Aashima Chopra",
        "qualification": "MBBS,DNB(ENT)",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/09/Aashima-1.png",
        "postings": [
            ("Lajpat Nagar", "ENT", "Senior Consultant - ENT"),
        ],
    },
    {
        "name": "Dr. Aastha Dewan",
        "qualification": None,
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/06/Dr.-Aastha-scaled.jpg",
        "postings": [
            (
                "Lajpat Nagar",
                "Psychiatry & Clinical Psychology",
                "Consultant - Psychiatry & Clinical Psychology",
            ),
            (
                "Karol Bagh",
                "Psychiatry & Clinical Psychology",
                "Consultant - Psychiatry & Clinical Psychology",
            ),
            (
                "Dwarka",
                "Psychiatry & Clinical Psychology",
                "Consultant - Psychiatry & Clinical Psychology",
            ),
        ],
    },
    {
        "name": "Dr. Aditya Sharma",
        "qualification": "MBBS,MD,DM( Cardiology)",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/10/aditya.png",
        "postings": [
            ("Karol Bagh", "Cardiology", "Consultant - Cardiology"),
        ],
    },
    {
        "name": "Dr. Aijaz Ilmi",
        "qualification": None,
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/04/Dr.-Aijaz-Ilmi-scaled.jpg",
        "postings": [
            (
                "Lajpat Nagar",
                "Preventive Health & Metabolic Diseases",
                "Preventive Health & Metabolic Diseases",
            ),
            (
                "Karol Bagh",
                "Preventive Health & Metabolic Diseases",
                "Preventive Health & Metabolic Diseases",
            ),
        ],
    },
    {
        "name": "Dr. Akshay Tyagi",
        "qualification": "MBBS, D.C.P, M.S (Orthopaedics)",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/07/Dr.-Akshay-scaled.jpg",
        "postings": [
            ("Lajpat Nagar", "Orthopaedics", "Consultant - Orthopaedics"),
            ("Karol Bagh", "Orthopaedics", "Consultant - Orthopaedics"),
        ],
    },
    {
        "name": "Dt. Ameera Raza",
        "qualification": None,
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/07/Ameera-scaled.jpg",
        "postings": [
            ("Lajpat Nagar", "Dietetics & Nutrition", "Dietetics & Nutrition"),
        ],
    },
    {
        "name": "Dr. Amrit Singh",
        "qualification": "BDS, MDS(Orthodontics)",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/09/Dr.-Amrit.JPG-1-scaled.jpeg",
        "postings": [
            ("Lajpat Nagar", "Dental", "Consultant - Dental"),
            ("Karol Bagh", "Dental", "Consultant - Dental"),
            ("Dwarka", "Dental", "Consultant - Dental"),
        ],
    },
    {
        "name": "Dr. Anil Kumar Singal",
        "qualification": "MBBS,MS (Surgery)",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/08/Dr.-Anil-scaled.jpg",
        "postings": [
            (
                "Lajpat Nagar",
                "General & MIS Surgery",
                "Senior Consultant - General & MIS Surgery",
            ),
        ],
    },
    {
        "name": "Dr. Anirban Biswas",
        "qualification": "MBBS,DLO",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/04/Dr.-Anirban-scaled.jpg",
        "postings": [
            (
                "Lajpat Nagar",
                "Vertigo and Balance Disorders",
                "Senior Consultant - Vertigo and Balance Disorders",
            ),
        ],
    },
    {
        "name": "Dr. Ashwani Chopra",
        "qualification": "MBBS,MD(Delhi), M.R.C.P (London)",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/09/Dr.-Ashwani.jpg.jpeg",
        "postings": [
            (
                "Lajpat Nagar",
                "Gastroenterologist",
                "Senior Consultant - Gastroenterologist",
            ),
        ],
    },
    {
        "name": "Dr. Bhawna Kumari",
        "qualification": "MBBS, MD Anesthesiology",
        "photo": None,
        "postings": [
            ("Lajpat Nagar", "Anaesthesiology", "Anaesthesiology"),
        ],
    },
    {
        "name": "Dr. Chandrakant Lahariya",
        "qualification": "MBBS,MD(Community Medicine)",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/10/lahar.jpg",
        "postings": [
            (
                "Lajpat Nagar",
                "Internal Medicine & Preventive care",
                "Internal Medicine & Preventive care",
            ),
        ],
    },
    {
        "name": "Dr. Deepa Gupta",
        "qualification": "MBBS, MD Psychiatry",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/08/deepa-1.jpeg",
        "postings": [
            (
                "Karol Bagh",
                "Psychiatry & Clinical Psychology",
                "Consultant - Psychiatry & Clinical Psychology",
            ),
        ],
    },
    {
        "name": "Dr. Diksha Sharma",
        "qualification": None,
        "photo": None,
        "postings": [
            (
                "Karol Bagh",
                "Psychiatry & Clinical Psychology",
                "Consultant - Psychiatry & Clinical Psychology",
            ),
        ],
    },
    {
        "name": "Dr. Divya Kumari",
        "qualification": "MBBS, MD - Obstetrics & Gynaecology",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/09/divya.jpeg",
        "postings": [
            ("Karol Bagh", "Gynaecology", "Consultant - Gynaecology"),
        ],
    },
    {
        "name": "Dr. G S Grewal",
        "qualification": "MBBS",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2025/08/Dr.-G-S-Grewal-scaled.jpg",
        "postings": [
            (
                "Lajpat Nagar",
                "Elder Care/Geriatric Medicine",
                "Director - Elder Care/Geriatric Medicine",
            ),
            (
                "Karol Bagh",
                "Elder Care/Geriatric Medicine",
                "Director - Elder Care/Geriatric Medicine",
            ),
        ],
    },
    {
        "name": "Dr. Irfan Khan",
        "qualification": None,
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/09/Dr.-Irfan-scaled.jpg",
        "postings": [
            (
                "Lajpat Nagar",
                "Physician & Diabetologist",
                "Consultant - Physician & Diabetologist",
            ),
        ],
    },
    {
        "name": "Dr. Jinal Katira",
        "qualification": "BDS, MDS (Pedodontics)",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/07/Dr.-Jinal.jpg",
        "postings": [
            ("Dwarka", "Dental", "Consultant - Dental"),
        ],
    },
    {
        "name": "Dt. Kanika Malhotra",
        "qualification": "MSC",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/05/Ms-Kanikka.jpg",
        "postings": [
            ("Lajpat Nagar", "Dietetics & Nutrition", "Dietetics & Nutrition"),
            ("Karol Bagh", "Dietetics & Nutrition", "Dietetics & Nutrition"),
        ],
    },
    {
        "name": "Dr. Karuna",
        "qualification": "MBBS, MD(General Medicine), DNB, MIMA, Certified Diabetologist(NHS England)",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/09/karuna-1.jpeg",
        "postings": [
            (
                "Dwarka",
                "Internal Medicine & Preventive care",
                "Consultant - Internal Medicine & Preventive care",
            ),
        ],
    },
    {
        "name": "Dt. Lavanya Gupta",
        "qualification": None,
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/08/Lavanya.jpg",
        "postings": [
            ("Lajpat Nagar", "Dietetics & Nutrition", "Dietetics & Nutrition"),
            ("Karol Bagh", "Dietetics & Nutrition", "Dietetics & Nutrition"),
        ],
    },
    {
        "name": "Dr. Maj Harshita Surange",
        "qualification": "MBBS, Diploma in Medical Radio-Diagnosis",
        "photo": None,
        "postings": [
            ("Lajpat Nagar", "Radiology", "Consultant - Radiology"),
            ("Karol Bagh", "Radiology", "Consultant - Radiology"),
            ("Dwarka", "Radiology", "Consultant - Radiology"),
        ],
    },
    {
        "name": "Dr. Maj Rakesh Kapoor",
        "qualification": "MBBS, MS(Opthal)",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/09/Dr.-Rakesh.jpg",
        "postings": [
            ("Dwarka", "Ophthalmology", "Ophthalmology"),
        ],
    },
    {
        "name": "Dr. Manoj",
        "qualification": None,
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/09/manoj.jpeg",
        "postings": [
            ("Karol Bagh", "Anaesthesiology", "Senior Consultant - Anaesthesiology"),
        ],
    },
    {
        "name": "Dr. Mansi Sidharth Dhende",
        "qualification": "MBBS, MS (Gen.Surgery), M.Ch Plastic Surgery/Plastic & Reconstructive Surgery",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/09/Mansi.png",
        "postings": [
            (
                "Lajpat Nagar",
                "Plastic & Reconstructive Surgery",
                "Consultant - Plastic & Reconstructive Surgery",
            ),
            (
                "Dwarka",
                "Plastic & Reconstructive Surgery",
                "Consultant - Plastic & Reconstructive Surgery",
            ),
        ],
    },
    {
        "name": "Dr. Mayur Anand",
        "qualification": "MBBS,MS (Ortho),Diploma in FIFA Sports Medicine",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/06/mayur.jpeg",
        "postings": [
            ("Lajpat Nagar", "Orthopedic & Spine", "Consultant - Orthopedic & Spine"),
        ],
    },
    {
        "name": "Dr. Meetali Singh",
        "qualification": "MBBS,MD(Gen.Med)",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/10/Meetali.jpg",
        "postings": [
            (
                "Lajpat Nagar",
                "Internal Medicine & Preventive care",
                "Senior Consultant - Internal Medicine & Preventive care",
            ),
        ],
    },
    {
        "name": "Dr. Mohammad Azam Zaidi",
        "qualification": "MBBS,MS/MD-Ophthalmology",
        "photo": None,
        "postings": [
            ("Lajpat Nagar", "Ophthalmology", "Consultant - Ophthalmology"),
        ],
    },
    {
        "name": "Prof.(Dr.) Mohsin Wali",
        "qualification": None,
        "photo": "https://pacificonehealth.com/wp-content/uploads/2025/08/Dr.-Mohsin-Wali-1-scaled.jpg",
        "postings": [
            (
                "Lajpat Nagar",
                "Internal Medicine & Preventive care",
                "Senior Consultant - Internal Medicine & Preventive care",
            ),
            (
                "Karol Bagh",
                "Internal Medicine & Preventive care",
                "Senior Consultant - Internal Medicine & Preventive care",
            ),
        ],
    },
    {
        "name": "Dr. Molisha Bhandari",
        "qualification": None,
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/05/Molisha-2.jpeg",
        "postings": [
            (
                "Lajpat Nagar",
                "Dermatology & Aesthetics",
                "Visiting Consultant - Dermatology & Aesthetics",
            ),
        ],
    },
    {
        "name": "Dr. Namita Verma Kala",
        "qualification": None,
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/08/Dr.-Namita-scaled.jpg",
        "postings": [
            (
                "Lajpat Nagar",
                "Vertigo and Balance Disorders",
                "Consultant - Vertigo and Balance Disorders",
            ),
        ],
    },
    {
        "name": "Dr. Naval Bhatia",
        "qualification": "MBBS,MS(Orthopaedics)",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/05/Dr.-Naval-Bhatia-scaled.jpg",
        "postings": [
            ("Lajpat Nagar", "Orthopedic & Spine", "Orthopedic & Spine"),
            ("Karol Bagh", "Orthopedic & Spine", "Orthopedic & Spine"),
        ],
    },
    {
        "name": "Dr. Neeta Kejriwal",
        "qualification": "MBBS, MD HOD Director Pediatrics",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/09/Dr.-Neeta.JPG-1-scaled.jpeg",
        "postings": [
            ("Dwarka", "Paediatrics", "Director - Paediatrics"),
        ],
    },
    {
        "name": "Prof.(Dr.) Nimesh Gajendradhar Desai",
        "qualification": "MBBS,DPM,MD(Psychiatry)",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/06/WhatsApp-Image-2026-07-02-at-4.50.07-PM.jpeg",
        "postings": [
            (
                "Lajpat Nagar",
                "Psychiatry & Clinical Psychology",
                "Consultant - Psychiatry & Clinical Psychology",
            ),
        ],
    },
    {
        "name": "Dr. P K Rathore",
        "qualification": "MBBS, MS(Otorhinolaryngology)",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/09/Rathore.jpg",
        "postings": [
            ("Dwarka", "ENT", "Director - ENT"),
        ],
    },
    {
        "name": "Dr. Pratik Lakhani",
        "qualification": None,
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/08/pratik.jpeg",
        "postings": [
            ("Karol Bagh", "Orthopaedics", "Senior Consultant - Orthopaedics"),
        ],
    },
    {
        "name": "Dr. Pritam Kitey",
        "qualification": "MBBS,MD,DrNB(Cardiology)",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/05/Dr.-Pritam.jpg",
        "postings": [
            ("Lajpat Nagar", "Cardiology", "Visiting Consultant - Cardiology"),
            ("Karol Bagh", "Cardiology", "Visiting Consultant - Cardiology"),
        ],
    },
    {
        "name": "Dr. Priyanka",
        "qualification": "MBBS, MS(Obs&Gynae), Diploma in Advanced Gynaecology Endoscopy",
        "photo": None,
        "postings": [
            ("Dwarka", "IVF", "Senior Consultant - IVF"),
        ],
    },
    {
        "name": "Dr. Priyanka Bindroo",
        "qualification": "MBBS",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/09/Dr.-Priyanka-Bindroo.JPG-scaled.jpeg",
        "postings": [
            ("Dwarka", "Pulmonology", "Consultant - Pulmonology"),
        ],
    },
    {
        "name": "Dr. Priyanka Hemrajani",
        "qualification": None,
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/09/hemrajani.jpeg",
        "postings": [
            (
                "Karol Bagh",
                "Dermatology & Aesthetics",
                "Senior Consultant - Dermatology & Aesthetics",
            ),
        ],
    },
    {
        "name": "Dt. Puja Sharma Vasisht",
        "qualification": "UG, DDPN",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/09/puja.jpeg",
        "postings": [
            (
                "Dwarka",
                "Dietetics & Nutrition",
                "Senior Consultant - Dietetics & Nutrition",
            ),
        ],
    },
    {
        "name": "Dr. Punam Singh",
        "qualification": "MBBS,MD (Dermatology)",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/10/Punam-1.jpg",
        "postings": [
            (
                "Karol Bagh",
                "Dermatology & Aesthetics",
                "Consultant - Dermatology & Aesthetics",
            ),
            (
                "Dwarka",
                "Dermatology & Aesthetics",
                "Consultant - Dermatology & Aesthetics",
            ),
        ],
    },
    {
        "name": "Dr. Rajeev Nangia",
        "qualification": "MBBS, MS(Oto-Rhino-Lary)",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/09/rajeev-1.jpeg",
        "postings": [
            ("Lajpat Nagar", "ENT", "Senior Consultant - ENT"),
            ("Karol Bagh", "ENT", "Senior Consultant - ENT"),
        ],
    },
    {
        "name": "Dr. Rajeev Sood",
        "qualification": None,
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/09/Sood-1.jpeg",
        "postings": [
            ("Lajpat Nagar", "Urology", "Senior Consultant - Urology"),
            ("Karol Bagh", "Urology", "Senior Consultant - Urology"),
        ],
    },
    {
        "name": "Dr. Rashmi Yadav",
        "qualification": "BAMS, MD(Ayurveda), PGP(Panchkarma)",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/09/Dr.-Rashmi.jpg",
        "postings": [
            ("Dwarka", "AYURVEDA (RSBK)", "AYURVEDA (RSBK)"),
        ],
    },
    {
        "name": "Dr. Richa Singh",
        "qualification": "MBBS,DNB (Neuro-Suregry)",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/08/Richa.jpeg",
        "postings": [
            ("Karol Bagh", "Neurosciences", "Senior Consultant - NEUROSCIENCES"),
        ],
    },
    {
        "name": "Dr. Ritika Kanwal Jindal",
        "qualification": "BDS,MDS(Orthodontics &Dentofacial Orthopaedics",
        "photo": None,
        "postings": [
            ("Lajpat Nagar", "Orthopaedics", "Senior Consultant - Orthopaedics"),
        ],
    },
    {
        "name": "Prof.(Dr.) RK Srivastava",
        "qualification": "MBBS,MS(Orthopaedics Surgery), DNB(Physical Med.&Rehab)",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/05/Dr.-RK-Srivastava-1-scaled.jpg",
        "postings": [
            ("Lajpat Nagar", "Orthopaedics", "Visiting Consultant - Orthopaedics"),
        ],
    },
    {
        "name": "Dr. S C Sharma",
        "qualification": "MBBS",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/10/subhash.jpeg",
        "postings": [
            (
                "Karol Bagh",
                "General Practitioner",
                "Senior Consultant - General Practitioner",
            ),
        ],
    },
    {
        "name": "Dr. Samiksha Gumber",
        "qualification": None,
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/06/Dr.-Samiksha-Gumber.JPG-1-scaled.jpeg",
        "postings": [
            ("Lajpat Nagar", "Dental", "Consultant - Dental"),
        ],
    },
    {
        "name": "Dr. Satish Kumar",
        "qualification": None,
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/08/Dr.-Satish-scaled.jpg",
        "postings": [
            (
                "Lajpat Nagar",
                "Preventive Health & Metabolic Diseases",
                "Preventive Health & Metabolic Diseases",
            ),
        ],
    },
    {
        "name": "Dr. Shadma Siddiqui",
        "qualification": "MBBS, MS(OBGY), FCG, Diploma in Reproductive Medicine & High Risk Pregnancy, IVF Specialist",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/09/shadma1.jpeg",
        "postings": [
            ("Dwarka", "Gynaecology", "Consultant - Gynaecology"),
        ],
    },
    {
        "name": "Dr. Shailendra Bhadoriya",
        "qualification": "MBBS,DA,Fellowship in Cardiothoracic Anaesthesiology",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/07/shail.jpeg",
        "postings": [
            ("Lajpat Nagar", "Cardiology", "Visiting Consultant - Cardiology"),
        ],
    },
    {
        "name": "Dr. Shikha Kumari",
        "qualification": None,
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/09/Shikha.png",
        "postings": [
            ("Lajpat Nagar", "General Practitioner", "General Practitioner"),
        ],
    },
    {
        "name": "Dr. Smriti Uppal",
        "qualification": "MBBS,DGO,DNB(OBST.&GYNAE)",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/04/Dr.-Smriti-Uppal-scaled.jpg",
        "postings": [
            ("Lajpat Nagar", "Gynaecology", "Senior Consultant - Gynaecology"),
            ("Karol Bagh", "Gynaecology", "Senior Consultant - Gynaecology"),
        ],
    },
    {
        "name": "Dr. Srishti Jain",
        "qualification": "MBBS, MD, DNB, MNAMS(Dermatology)",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/09/Dr.-Srishti-Jain.jpg",
        "postings": [
            (
                "Dwarka",
                "Dermatology & Aesthetics",
                "Consultant - Dermatology & Aesthetics",
            ),
        ],
    },
    {
        "name": "Dr. Srishti Kumar",
        "qualification": "BDS, Clinical Preceptorship in Oral Medicine (Rutgers University- Newark)",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/07/WhatsApp-Image-2026-10-13-at-12.25.04-PM.jpeg",
        "postings": [
            ("Dwarka", "Dental", "Consultant - Dental"),
        ],
    },
    {
        "name": "Dr. Sudha Prasad",
        "qualification": "MBBS, MD(Obs&Gynae), FICOG, FICMCH",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/09/Dr.-Sudha.JPG-scaled.jpeg",
        "postings": [
            ("Dwarka", "IVF", "Senior Consultant - IVF"),
        ],
    },
    {
        "name": "Ms. Sumedha Ghosh",
        "qualification": None,
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/10/sumedha.png",
        "postings": [
            (
                "Karol Bagh",
                "Psychiatry & Clinical Psychology",
                "Psychiatry & Clinical Psychology",
            ),
        ],
    },
    {
        "name": "Dr. Sumit Kumar",
        "qualification": "MBBS,MD (Internal Medicine) ,DM IN Cardiology",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/10/sumit-1.png",
        "postings": [
            ("Karol Bagh", "Cardiology", "Consultant - Cardiology"),
        ],
    },
    {
        "name": "Dr. Sunil Sareen",
        "qualification": None,
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/08/sunil1.jpeg",
        "postings": [
            ("Karol Bagh", "Paediatrics", "Consultant - Paediatrics"),
        ],
    },
    {
        "name": "Dr. Suresh Kumar Poddar",
        "qualification": "GI Surgeon(MBBS,MS(Gen.Surgery)",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/06/Dr.-SK-Poddar-scaled.jpg",
        "postings": [
            (
                "Lajpat Nagar",
                "GI & Bariatric Surgery",
                "Consultant - GI & Bariatric Surgery",
            ),
        ],
    },
    {
        "name": "Dr. Tanya Jaswal",
        "qualification": None,
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/09/Dr.-Tanya.jpg",
        "postings": [
            ("Karol Bagh", "Dental", "Consultant - Dental"),
        ],
    },
    {
        "name": "Dr. Tilottama Nischal",
        "qualification": None,
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/09/tollitama.jpeg",
        "postings": [
            (
                "Karol Bagh",
                "Internal Medicine & Preventive care",
                "Consultant - Internal Medicine & Preventive care",
            ),
        ],
    },
    {
        "name": "Dr. V K Singh",
        "qualification": "MBBS, M.S.- General surgery",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/10/Vinod.jpg",
        "postings": [
            (
                "Karol Bagh",
                "General & MIS Surgery",
                "Consultant - General & MIS Surgery",
            ),
        ],
    },
    {
        "name": "Dr. Vikrant Bhardwaj",
        "qualification": None,
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/06/dr-vikrant-bhardwaj.jpg.jpeg",
        "postings": [
            (
                "Lajpat Nagar",
                "Physiotherapy & Rehabilitation",
                "Consultant - Physiotherapy & Rehabilitation",
            ),
            (
                "Karol Bagh",
                "Physiotherapy & Rehabilitation",
                "Consultant - Physiotherapy & Rehabilitation",
            ),
            (
                "Dwarka",
                "Physiotherapy & Rehabilitation",
                "Consultant - Physiotherapy & Rehabilitation",
            ),
        ],
    },
    {
        "name": "Dr. Zahid Ali",
        "qualification": "MBBS, DCH, FCCS (USA)",
        "photo": "https://pacificonehealth.com/wp-content/uploads/2026/06/Dr.-Zahid-Ali-1-scaled.jpg",
        "postings": [
            ("Lajpat Nagar", "Paediatrics", "Senior Consultant - Paediatrics"),
        ],
    },
]
