# PEAR_logging_service
Central Logging Service for PEAR Microservices

### Setting Up Your Environment
1. Create a Conda Virtual Environment:
```bash
# This is to set the python version in your conda environment to 3.9.19
conda create -n pear_logging_service python=3.9.19
conda activate pear_logging_service
```
### Install the required dependencies
```bash
#install the necessary requirements in the conda environment
pip install -r requirements.txt
```

### Environment Configuration
Make sure to create a .env file in the PEAR_logging_service folder. For instructions on how to configure this file, refer to the Confluence page:

Click on the link to be redirect to the confluence page -->[Confluence page](https://fyppear.atlassian.net/wiki/spaces/FP/pages/132939777/Environment+Configuration+-+.env+File)


### Running the Application 
After the installation is completed, run the application on your machine
```bash
uvicorn app.main:app --reload
```
